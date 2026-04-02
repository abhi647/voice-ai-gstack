"""
Twilio Media Streams WebSocket handler — pure-Twilio voice AI pipeline.

Architecture:
  POST /twilio/voice (calls.py) returns <Connect><Stream url="wss://.../twilio/stream">
  Twilio opens a WebSocket here and streams bidirectional μ-law 8 kHz audio.

Per-call pipeline:
  Caller audio → Deepgram streaming STT → Anthropic Claude → ElevenLabs TTS → Twilio audio

Barge-in:
  Deepgram interim transcripts signal that the user started speaking.
  Any in-progress TTS task is cancelled and Twilio's audio buffer is cleared.

Call lifecycle:
  connected → start → media (N) → stop
  We finalize the call record via POST /internal/finalize_call on stop/disconnect.
"""

import asyncio
import base64
import json
import logging
import os
from typing import AsyncGenerator

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.agent.disclosures import get_disclosure
from app.agent.prompts import build_system_prompt
from app.agent.state import ConversationContext, ConversationState
from app.database import AsyncSessionLocal
from app.models.practice import Practice
from app.models.practice_config import PracticeConfig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/twilio", tags=["stream"])


# ─────────────────────────────────────────────────────────────────────────────
# ElevenLabs TTS — direct httpx streaming, no SDK needed
# ─────────────────────────────────────────────────────────────────────────────

_EL_STREAM_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"


async def _tts_stream(text: str, voice_id: str, api_key: str) -> AsyncGenerator[bytes, None]:
    """
    Async generator: yields μ-law 8 kHz audio chunks from ElevenLabs.

    Using the /stream endpoint for sub-second first-byte latency.
    ulaw_8000 output matches Twilio's expected audio format exactly —
    no resampling or encoding conversion needed.
    """
    # output_format is a query parameter in the ElevenLabs API, NOT a body field.
    # Sending it in the body causes it to be silently ignored and ElevenLabs returns
    # MP3 by default — raw MP3 played as μ-law sounds like pure noise on Twilio.
    url = _EL_STREAM_URL.format(voice_id=voice_id) + "?output_format=ulaw_8000"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": "eleven_turbo_v2_5",
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.80},
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            async for chunk in resp.aiter_bytes(4096):
                if chunk:
                    yield chunk


# ─────────────────────────────────────────────────────────────────────────────
# Anthropic LLM
# ─────────────────────────────────────────────────────────────────────────────


async def _claude_respond(
    messages: list[dict],
    system: str,
    model: str,
    api_key: str,
) -> str:
    """Call Claude and return the complete text response."""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=api_key)
    async with client.messages.stream(
        model=model,
        system=system,
        messages=messages,
        max_tokens=256,
    ) as stream:
        return await stream.get_final_text()


# ─────────────────────────────────────────────────────────────────────────────
# Per-call handler
# ─────────────────────────────────────────────────────────────────────────────


class CallHandler:
    """
    Owns one Twilio Media Stream WebSocket session.

    One instance per inbound call. Wired to:
      - Deepgram (STT)    — streaming connection, one per call
      - Anthropic (LLM)   — stateless HTTP, called once per turn
      - ElevenLabs (TTS)  — streaming HTTP, called once per turn
    """

    def __init__(
        self,
        websocket: WebSocket,
        stream_sid: str,
        call_sid: str,
        patient_phone: str,
        practice: Practice,
        config: PracticeConfig,
    ):
        self.ws = websocket
        self.stream_sid = stream_sid
        self.call_sid = call_sid
        self.config = config

        self.conv = ConversationContext(
            practice_id=str(practice.id),
            practice_name=practice.name,
            practice_state=practice.state,
            practice_timezone=practice.timezone,
            call_sid=call_sid,
            patient_phone=patient_phone,
            escalation_number=practice.escalation_number or "",
            staff_email=practice.staff_email,
            ehr_adapter=config.ehr_adapter,
        )
        self.conv.booking.patient_phone = patient_phone

        # Claude conversation history (grows with each turn)
        self.messages: list[dict] = []
        self.system_prompt = build_system_prompt(
            practice.name, practice.state, ConversationState.GREETING, config
        )

        # Current TTS playback task (cancelled on barge-in)
        self._speaking_task: asyncio.Task | None = None
        self._speaking = False

        # Deepgram live connection
        self._dg_conn = None

        # Credentials from environment
        self._el_key = os.environ.get("ELEVENLABS_API_KEY", "")
        self._anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self._deepgram_key = os.environ.get("DEEPGRAM_API_KEY", "")

    # ── lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> bool:
        """
        Connect Deepgram and deliver the opening greeting.

        Returns True if the call should continue (WebSocket is still open).
        Returns False if the call was ended (e.g. after-hours) and the caller
        should break out of the message loop.
        """
        # After-hours check — play message and end call without entering the loop
        if (
            self.config.after_hours_message
            and not self.config.business_hours.is_open_now(self.conv.practice_timezone)
        ):
            logger.info(f"After-hours call for practice {self.conv.practice_id}")
            await self._play_text(self.config.after_hours_message)
            # Do NOT call ws.close() explicitly — Azure's proxy fragments the CLOSE
            # control frame, causing Twilio error 31924. Instead just return False
            # and let the endpoint function exit, which closes the connection cleanly.
            return False  # signal to caller: stop loop, endpoint will close WS on exit

        await self._connect_deepgram()

        greeting = self._build_greeting()
        self.conv.append_transcript("AGENT", greeting)
        self.messages.append({"role": "assistant", "content": greeting})
        self.conv.transition(ConversationState.IDENTIFY_PATIENT)

        self._speaking_task = asyncio.create_task(self._play_text(greeting))
        return True

    async def on_audio(self, audio: bytes) -> None:
        """Forward each incoming audio chunk to Deepgram."""
        if self._dg_conn is not None:
            await self._dg_conn.send(audio)

    async def stop(self) -> None:
        """Call ended — clean up and persist the call record."""
        if self._speaking_task and not self._speaking_task.done():
            self._speaking_task.cancel()
        if self._dg_conn is not None:
            try:
                await self._dg_conn.finish()
            except Exception:
                pass
        await self._finalize()

    # ── private ───────────────────────────────────────────────────────────────

    def _build_greeting(self) -> str:
        disclosure = get_disclosure(self.conv.practice_state, self.config.sms_enabled)
        return (
            f"Thank you for calling {self.conv.practice_name}. "
            f"{disclosure} "
            f"My name is {self.config.agent_name}. How can I help you today?"
        )

    async def _connect_deepgram(self) -> None:
        """Open a Deepgram streaming connection and register the transcript callback."""
        from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents  # lazy import

        dg = DeepgramClient(self._deepgram_key)
        conn = dg.listen.asyncwebsocket.v("1")

        # Closure captures self — the first arg (_conn) is the Deepgram WS connection.
        async def on_message(_conn, result, **kwargs) -> None:
            try:
                alt = result.channel.alternatives[0]
                text = alt.transcript.strip()
                if not text:
                    return
                if result.is_final:
                    await self._on_final_transcript(text)
                elif self._speaking:
                    # Interim result while agent is speaking — barge-in detected.
                    logger.debug(f"Barge-in interim: '{text}'")
                    await self._interrupt()
            except Exception as exc:
                logger.error(f"Transcript callback error: {exc}")

        async def on_error(_conn, error, **kwargs) -> None:
            logger.error(f"Deepgram error: {error}")

        conn.on(LiveTranscriptionEvents.Transcript, on_message)
        conn.on(LiveTranscriptionEvents.Error, on_error)

        options = LiveOptions(
            model="nova-2",
            language="en-US",
            encoding="mulaw",
            sample_rate=8000,
            channels=1,
            endpointing=400,       # ms of silence → end of utterance
            interim_results=True,  # needed for barge-in detection
            smart_format=True,
        )
        started = await conn.start(options)
        if not started:
            raise RuntimeError("Failed to connect to Deepgram")

        self._dg_conn = conn

    async def _on_final_transcript(self, text: str) -> None:
        """User finished speaking. Interrupt agent if needed, then generate a reply."""
        logger.info(f"PATIENT [{self.call_sid}]: {text}")
        self.conv.append_transcript("PATIENT", text)

        # Interrupt any ongoing agent speech
        await self._interrupt()

        # Escalation keyword?
        keyword = self.conv.check_for_escalation_keyword(text)
        if keyword:
            logger.info(f"Escalation keyword '{keyword}' — transferring")
            self.conv.escalation_reason = f"keyword: {keyword}"
            self.conv.transition(ConversationState.ESCALATING)
            asyncio.create_task(self._trigger_escalation())
            return

        # 4-minute timeout?
        if self.conv.should_escalate_due_to_timeout():
            self.conv.escalation_reason = "timeout: 4 minutes without resolution"
            self.conv.transition(ConversationState.ESCALATING)
            asyncio.create_task(self._trigger_escalation())
            return

        # Normal turn: Claude → ElevenLabs → Twilio
        self._speaking_task = asyncio.create_task(self._respond(text))

    async def _respond(self, user_text: str) -> None:
        """Claude → ElevenLabs → stream audio to Twilio."""
        self.messages.append({"role": "user", "content": user_text})
        try:
            # Rebuild system prompt for current conversation state
            system = build_system_prompt(
                self.conv.practice_name,
                self.conv.practice_state,
                self.conv.state,
                self.config,
            )

            reply = await _claude_respond(
                messages=self.messages,
                system=system,
                model=self.config.llm_model,
                api_key=self._anthropic_key,
            )
            logger.info(f"AGENT  [{self.call_sid}]: {reply}")
            self.conv.append_transcript("AGENT", reply)
            self.messages.append({"role": "assistant", "content": reply})

            await self._play_text(reply)

        except asyncio.CancelledError:
            pass  # barge-in cancelled this task
        except Exception as exc:
            logger.error(f"_respond error [{self.call_sid}]: {exc}")

    async def _play_text(self, text: str) -> None:
        """TTS text and stream μ-law chunks to Twilio."""
        self._speaking = True
        try:
            async for chunk in _tts_stream(text, self.config.tts_voice_id, self._el_key):
                payload = base64.b64encode(chunk).decode()
                await self.ws.send_text(
                    json.dumps({
                        "event": "media",
                        "streamSid": self.stream_sid,
                        "media": {"payload": payload},
                    })
                )
        except asyncio.CancelledError:
            # Send clear so Twilio stops playing any already-buffered audio
            await self._send_clear()
            raise
        except WebSocketDisconnect:
            # Caller hung up while we were speaking — propagate so the loop exits cleanly
            raise
        except Exception as exc:
            logger.error(f"_play_text error [{self.call_sid}]: {exc}")
        finally:
            self._speaking = False

    async def _interrupt(self) -> None:
        """Cancel current TTS task and clear Twilio's audio buffer."""
        if self._speaking_task and not self._speaking_task.done():
            self._speaking_task.cancel()
            try:
                await self._speaking_task
            except (asyncio.CancelledError, Exception):
                pass
        self._speaking = False
        await self._send_clear()

    async def _send_clear(self) -> None:
        """Tell Twilio to discard any buffered (not-yet-played) audio."""
        try:
            await self.ws.send_text(
                json.dumps({"event": "clear", "streamSid": self.stream_sid})
            )
        except Exception:
            pass  # WebSocket may already be closed

    async def _trigger_escalation(self) -> None:
        """Tell the patient we're transferring, then POST to /internal/escalate."""
        hold_text = (
            "Let me connect you with our team right now. Please hold for just a moment."
        )
        self.conv.append_transcript("AGENT", hold_text)
        await self._interrupt()
        await self._play_text(hold_text)

        escalation_summary = self._build_escalation_summary()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    "http://localhost:8000/internal/escalate",
                    json={
                        "call_sid": self.conv.call_sid,
                        "practice_id": self.conv.practice_id,
                        "patient_name": self.conv.booking.patient_name,
                        "patient_phone": self.conv.patient_phone,
                        "reason": self.conv.escalation_reason,
                        "summary": escalation_summary,
                    },
                )
        except Exception as exc:
            logger.error(f"Escalation POST failed [{self.conv.call_sid}]: {exc}")

        self.conv.transition(ConversationState.TRANSFERRED)

    def _build_escalation_summary(self) -> str:
        name = self.conv.booking.patient_name or "a patient"
        reason = self.conv.escalation_reason or "requested to speak with someone"
        parts = [f"Transferring {name}"]
        if self.conv.booking.service_type:
            parts.append(f"calling about {self.conv.booking.service_type}")
        parts.append(f"reason: {reason}")
        return ", ".join(parts) + "."

    async def _finalize(self) -> None:
        """POST the completed call record to /internal/finalize_call."""
        def _disposition() -> str:
            if self.conv.state == ConversationState.TRANSFERRED:
                return "ESCALATED"
            if self.conv.state == ConversationState.COMPLETE:
                return "BOOKING_CAPTURED" if self.conv.booking.is_complete() else "FAQ_ONLY"
            return "HUNG_UP"

        payload = {
            "call_sid": self.conv.call_sid,
            "practice_id": self.conv.practice_id,
            "practice_name": self.conv.practice_name,
            "practice_timezone": self.conv.practice_timezone,
            "escalation_number": self.conv.escalation_number,
            "staff_email": self.conv.staff_email,
            "ehr_adapter": self.conv.ehr_adapter,
            "patient_phone": self.conv.patient_phone,
            "started_at": self.conv.started_at.isoformat(),
            "disposition": _disposition(),
            "patient_name": self.conv.booking.patient_name,
            "requested_time": self.conv.booking.requested_time,
            "service_type": self.conv.booking.service_type,
            "notes": self.conv.booking.notes,
            "transcript": self.conv.full_transcript(),
            "twilio_recording_url": None,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "http://localhost:8000/internal/finalize_call",
                    json=payload,
                )
                resp.raise_for_status()
                logger.info(f"Call finalized [{self.call_sid}]: {resp.json()}")
        except Exception as exc:
            # Last resort: log transcript so PHI is never silently lost
            logger.error(
                f"finalize_call failed [{self.call_sid}]: {exc}",
                extra={
                    "call_sid": self.call_sid,
                    "transcript": self.conv.full_transcript(),
                },
            )


# ─────────────────────────────────────────────────────────────────────────────
# WebSocket endpoint
# ─────────────────────────────────────────────────────────────────────────────


@router.websocket("/stream")
async def media_stream(websocket: WebSocket) -> None:
    """
    Twilio Media Streams WebSocket endpoint.

    Twilio opens this connection when the call is answered and keeps it open
    for the duration of the call. Messages are line-delimited JSON.

    Event sequence per call:
      connected → start → media × N → stop
    """
    await websocket.accept()

    handler: CallHandler | None = None

    try:
        async for raw in websocket.iter_text():
            msg = json.loads(raw)
            event = msg.get("event")

            if event == "connected":
                # Acknowledgement only — no action needed
                continue

            elif event == "start":
                start = msg["start"]
                params = start.get("customParameters", {})
                stream_sid = start["streamSid"]
                call_sid = start["callSid"]
                patient_phone = params.get("patient_phone", "unknown")
                practice_id = params.get("practice_id")

                # Load practice config from DB
                practice = await _load_practice(practice_id)
                if practice is None:
                    logger.warning(
                        f"Practice {practice_id!r} not found — closing stream {stream_sid}"
                    )
                    await websocket.close()
                    return

                config = practice.get_config()
                handler = CallHandler(
                    websocket=websocket,
                    stream_sid=stream_sid,
                    call_sid=call_sid,
                    patient_phone=patient_phone,
                    practice=practice,
                    config=config,
                )
                should_continue = await handler.start()
                if not should_continue:
                    break  # after-hours or similar — WebSocket already closed

            elif event == "media":
                if handler is not None:
                    audio = base64.b64decode(msg["media"]["payload"])
                    await handler.on_audio(audio)

            elif event == "stop":
                break

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for call {handler.call_sid if handler else '?'}")
    except RuntimeError as exc:
        # Starlette 1.x raises RuntimeError (not WebSocketDisconnect) when you try to read
        # from a WebSocket that was already closed via ws.close().  Treat as normal disconnect.
        logger.info(
            f"WebSocket closed (RuntimeError) for call {handler.call_sid if handler else '?'}: {exc}"
        )
    except Exception as exc:
        logger.error(f"media_stream error: {exc}", exc_info=True)
    finally:
        if handler is not None:
            await handler.stop()


async def _load_practice(practice_id: str | None) -> "Practice | None":
    """Look up a Practice by UUID string. Returns None if not found or invalid."""
    import uuid

    if not practice_id:
        return None
    try:
        uid = uuid.UUID(practice_id)
    except ValueError:
        return None

    async with AsyncSessionLocal() as db:
        from sqlalchemy.sql import select
        result = await db.execute(
            select(Practice).where(Practice.id == uid, Practice.is_active == True)
        )
        return result.scalar_one_or_none()
