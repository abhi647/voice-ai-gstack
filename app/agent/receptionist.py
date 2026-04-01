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
    practice_timezone = metadata.get("practice_timezone", "America/New_York")
    call_sid = metadata.get("call_sid", "unknown")
    patient_phone = metadata.get("patient_phone", "unknown")
    escalation_number = metadata.get("escalation_number", "")
    staff_email = metadata.get("staff_email")
    stt_provider = metadata.get("stt_provider", "deepgram")
    tts_provider = metadata.get("tts_provider", "elevenlabs")

    # Deserialize per-practice config (agent name, services, EHR adapter, voice, etc.)
    from app.models.practice_config import PracticeConfig
    config = PracticeConfig.from_dict(metadata.get("config"))

    # Initialize conversation context (in-memory for this call)
    conv = ConversationContext(
        practice_id=practice_id,
        practice_name=practice_name,
        practice_state=practice_state,
        practice_timezone=practice_timezone,
        call_sid=call_sid,
        patient_phone=patient_phone,
        escalation_number=escalation_number,
        staff_email=staff_email,
        ehr_adapter=config.ehr_adapter,
    )
    conv.booking.patient_phone = patient_phone

    # After-hours check — if outside business hours, deliver message and hang up
    if config.after_hours_message and not config.business_hours.is_open_now(practice_timezone):
        logger.info(f"After-hours call for practice {practice_id} — delivering message")
        assistant_ah = VoiceAssistant(
            vad=silero.VAD.load(),
            stt=deepgram.STT(model="nova-2", language="en-US",
                             api_key=os.environ.get("DEEPGRAM_API_KEY", "")),
            llm=anthropic.LLM(model=config.llm_model,
                               api_key=os.environ.get("ANTHROPIC_API_KEY", "")),
            tts=_build_tts(tts_provider, config),
        )
        assistant_ah.start(ctx.room)
        await assistant_ah.say(config.after_hours_message, allow_interruptions=False)
        return

    # Build initial system prompt for GREETING state (parameterized by PracticeConfig)
    initial_chat_ctx = llm.ChatContext().append(
        role="system",
        text=build_system_prompt(practice_name, practice_state, ConversationState.GREETING, config),
    )

    # STT: provider selected from practice config
    stt = _build_stt(stt_provider)

    # LLM: Claude (BAA signed with Anthropic) — model from practice config
    lm = anthropic.LLM(
        model=config.llm_model,
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    )

    # TTS: provider + voice selected from practice config
    tts = _build_tts(tts_provider, config)

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
    disclosure = get_disclosure(practice_state, sms_enabled=config.sms_enabled)
    greeting = (
        f"Thank you for calling {practice_name}. {disclosure} "
        f"My name is {config.agent_name}. How can I help you today?"
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


async def _finalize_call(conv: ConversationContext, twilio_recording_url: str | None = None) -> None:
    """
    POST call record to FastAPI /internal/finalize_call.
    FastAPI writes to PostgreSQL and uploads to S3.
    """
    import httpx

    payload = {
        "call_sid": conv.call_sid,
        "practice_id": conv.practice_id,
        "practice_name": conv.practice_name,
        "practice_timezone": conv.practice_timezone,
        "escalation_number": conv.escalation_number,
        "staff_email": conv.staff_email,
        "ehr_adapter": conv.ehr_adapter,
        "patient_phone": conv.patient_phone,
        "started_at": conv.started_at.isoformat(),
        "disposition": _disposition(conv),
        "patient_name": conv.booking.patient_name,
        "requested_time": conv.booking.requested_time,
        "service_type": conv.booking.service_type,
        "notes": conv.booking.notes,
        "transcript": conv.full_transcript(),
        "twilio_recording_url": twilio_recording_url,
    }

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "http://localhost:8000/internal/finalize_call",
                json=payload,
                timeout=30.0,
            )
            resp.raise_for_status()
            logger.info(f"Call finalized: {resp.json()}")
    except Exception as e:
        # Last-resort: log the full transcript so PHI isn't silently lost
        logger.error(
            "finalize_call POST failed — logging transcript locally",
            extra={
                "call_sid": conv.call_sid,
                "practice_id": conv.practice_id,
                "disposition": _disposition(conv),
                "error": str(e),
                "transcript": conv.full_transcript(),
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


def _build_stt(stt_provider: str):
    """Return the STT plugin for the given provider name."""
    if stt_provider == "sarvam":
        # Sarvam-AI STT — multilingual (Hindi, Tamil, Telugu, etc.)
        # Swap in when practice serves non-English speaking patients
        # from livekit.plugins import sarvam  # TODO: add when sarvam plugin ships
        logger.warning("Sarvam STT not yet available — falling back to Deepgram")

    # Default: Deepgram nova-2 (English, low-latency, HIPAA BAA signed)
    return deepgram.STT(
        model="nova-2",
        language="en-US",
        api_key=os.environ.get("DEEPGRAM_API_KEY", ""),
    )


def _build_tts(tts_provider: str, config):
    """Return the TTS plugin for the given provider name and practice config."""
    if tts_provider == "cartesia":
        # Cartesia — alternative TTS with different voice options
        # from livekit.plugins import cartesia  # TODO: wire up when needed
        logger.warning("Cartesia TTS not yet wired — falling back to ElevenLabs")

    # Default: ElevenLabs (BAA confirmed, low-latency turbo model)
    return elevenlabs.TTS(
        api_key=os.environ.get("ELEVENLABS_API_KEY", ""),
        voice_id=config.tts_voice_id,
        model_id="eleven_turbo_v2",
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
