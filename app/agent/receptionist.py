"""
ReceptionistAgent — LiveKit Agents voice pipeline.

Architecture:
  Twilio PSTN → LiveKit Room → ReceptionistAgent
    STT: Deepgram streaming (English; Sarvam-ready via provider swap)
    LLM: Claude (Anthropic) — BAA signed
    TTS: ElevenLabs — BAA confirmed
    State machine: ConversationContext (app/agent/state.py)

Escalation path:
  Agent detects trigger → POST /internal/escalate → FastAPI → Twilio REST API
  (warm transfer with whisper leg — see app/routers/internal.py)

Call end:
  Agent calls finalize_call() → writes transcript to PostgreSQL + audio to S3
"""

import logging
import os

from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli, llm
from livekit.agents.voice_assistant import VoiceAssistant
from livekit.plugins import anthropic, deepgram, elevenlabs, openai, silero

from app.agent.disclosures import get_disclosure
from app.agent.prompts import build_system_prompt
from app.agent.state import ConversationContext, ConversationState

logger = logging.getLogger(__name__)


async def entrypoint(ctx: JobContext) -> None:
    """
    LiveKit calls this for every new agent job (one per inbound call).

    ctx.job.metadata contains the practice config, serialized as JSON by the
    Twilio webhook handler when it creates the LiveKit room.
    """
    import json

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    # Parse practice metadata injected by the Twilio webhook
    metadata = json.loads(ctx.job.metadata or "{}")
    practice_id = metadata.get("practice_id", "unknown")
    practice_name = metadata.get("practice_name", "the practice")
    practice_state = metadata.get("practice_state", "NY")
    call_sid = metadata.get("call_sid", "unknown")
    patient_phone = metadata.get("patient_phone", "unknown")

    # Initialize conversation context (in-memory for this call)
    conv = ConversationContext(
        practice_id=practice_id,
        practice_name=practice_name,
        practice_state=practice_state,
        call_sid=call_sid,
        patient_phone=patient_phone,
    )
    conv.booking.patient_phone = patient_phone

    # Build initial system prompt for GREETING state
    initial_chat_ctx = llm.ChatContext().append(
        role="system",
        text=build_system_prompt(practice_name, practice_state, ConversationState.GREETING),
    )

    # STT: Deepgram streaming (provider abstraction — swap to Sarvam here later)
    stt = deepgram.STT(
        model="nova-2",
        language="en-US",
        api_key=os.environ.get("DEEPGRAM_API_KEY", ""),
    )

    # LLM: Claude (BAA signed with Anthropic)
    lm = anthropic.LLM(
        model="claude-sonnet-4-6",
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    )

    # TTS: ElevenLabs (BAA confirmed)
    tts = elevenlabs.TTS(
        api_key=os.environ.get("ELEVENLABS_API_KEY", ""),
        voice_id="21m00Tcm4TlvDq8ikWAM",  # Rachel — warm, professional
        model_id="eleven_turbo_v2",  # low-latency model
    )

    # VAD: Silero for end-of-speech detection
    vad = silero.VAD.load()

    assistant = VoiceAssistant(
        vad=vad,
        stt=stt,
        llm=lm,
        tts=tts,
        chat_ctx=initial_chat_ctx,
    )

    assistant.start(ctx.room)

    # Deliver HIPAA disclosure as the very first utterance (verbatim, not via LLM)
    disclosure = get_disclosure(practice_state)
    greeting = (
        f"Thank you for calling {practice_name}. {disclosure} "
        f"My name is Aria. How can I help you today?"
    )
    await assistant.say(greeting, allow_interruptions=True)
    conv.append_transcript("AGENT", greeting)
    conv.transition(ConversationState.IDENTIFY_PATIENT)

    # Register hooks for monitoring conversation turns
    @assistant.on("user_speech_committed")
    def on_user_speech(msg: llm.ChatMessage) -> None:
        text = msg.content or ""
        conv.append_transcript("PATIENT", text)

        # Check escalation keywords
        keyword = conv.check_for_escalation_keyword(text)
        if keyword:
            logger.info(f"Escalation keyword detected: '{keyword}' — triggering transfer")
            conv.escalation_reason = f"keyword: {keyword}"
            conv.transition(ConversationState.ESCALATING)
            # Schedule escalation — runs outside this sync callback
            import asyncio
            asyncio.create_task(_trigger_escalation(conv, assistant))

        # Check 4-minute timeout
        if conv.should_escalate_due_to_timeout():
            logger.info("Call timeout — triggering escalation")
            conv.escalation_reason = "timeout: 4 minutes without resolution"
            conv.transition(ConversationState.ESCALATING)
            import asyncio
            asyncio.create_task(_trigger_escalation(conv, assistant))

    @assistant.on("agent_speech_committed")
    def on_agent_speech(msg: llm.ChatMessage) -> None:
        conv.append_transcript("AGENT", msg.content or "")

    # Wait for the call to end
    await assistant.run()

    # Call ended — finalize (write to DB + S3)
    await _finalize_call(conv)


async def _trigger_escalation(conv: ConversationContext, assistant: VoiceAssistant) -> None:
    """
    Signal FastAPI to initiate a Twilio warm transfer with whisper leg.
    The actual transfer is handled by POST /internal/escalate.
    """
    import httpx

    escalation_summary = _build_escalation_summary(conv)

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                "http://localhost:8000/internal/escalate",
                json={
                    "call_sid": conv.call_sid,
                    "practice_id": conv.practice_id,
                    "patient_name": conv.booking.patient_name,
                    "patient_phone": conv.patient_phone,
                    "reason": conv.escalation_reason,
                    "summary": escalation_summary,
                },
                timeout=10.0,
            )
    except Exception as e:
        logger.error(f"Failed to trigger escalation: {e}")

    # Tell the patient we're connecting them
    await assistant.say(
        "Let me connect you with our team right now. Please hold for just a moment.",
        allow_interruptions=False,
    )
    conv.transition(ConversationState.TRANSFERRED)


def _build_escalation_summary(conv: ConversationContext) -> str:
    """Build the whisper text read to the receiving human before the patient is bridged."""
    name = conv.booking.patient_name or "a patient"
    reason = conv.escalation_reason or "requested to speak with someone"
    service = conv.booking.service_type or ""
    time_req = conv.booking.requested_time or ""

    parts = [f"Transferring {name}"]
    if service:
        parts.append(f"calling about {service}")
    if time_req:
        parts.append(f"requested {time_req}")
    parts.append(f"reason: {reason}")

    return ", ".join(parts) + "."


async def _finalize_call(conv: ConversationContext) -> None:
    """
    Write call record to PostgreSQL and transcript to S3.
    Called when the LiveKit session ends.
    """
    # TODO(v0.2): implement DB write + S3 upload
    # For now: log the transcript so nothing is lost
    logger.info(
        "Call ended",
        extra={
            "call_sid": conv.call_sid,
            "practice_id": conv.practice_id,
            "state": conv.state,
            "disposition": _disposition(conv),
            "duration_seconds": int(conv.elapsed_seconds()),
            "patient_name": conv.booking.patient_name,
        },
    )


def _disposition(conv: ConversationContext) -> str:
    if conv.state == ConversationState.TRANSFERRED:
        return "ESCALATED"
    if conv.state == ConversationState.COMPLETE:
        if conv.booking.is_complete():
            return "BOOKING_CAPTURED"
        return "FAQ_ONLY"
    return "HUNG_UP"


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
