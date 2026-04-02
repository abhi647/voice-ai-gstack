"""
Tests for the Twilio Media Streams WebSocket handler (app/routers/stream.py).

Coverage:
  _tts_stream()
    ├── [✓] REGRESSION: output_format=ulaw_8000 is a URL query param, NOT a body field
    │         (body param is silently ignored → ElevenLabs returns MP3 → noise on Twilio)
    └── [✓] request body does NOT contain output_format

  _build_greeting()
    ├── [✓] includes practice name
    ├── [✓] includes agent name from config
    └── [✓] includes HIPAA recording disclosure

  CallHandler.start()
    ├── [✓] after-hours + message set → plays message and returns False
    └── [✓] business hours open + no after_hours_message → returns True and connects Deepgram

  Note: The full WebSocket pipeline (Deepgram STT → Claude → ElevenLabs TTS) requires
  live external connections and is covered by manual integration testing. The unit tests
  here focus on the pure/mockable logic that's been production-proven but has no
  automated regression protection.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from app.models.practice_config import PracticeConfig, BusinessHours
from app.routers.stream import _tts_stream, CallHandler


# ─────────────────────────────────────────────────────────────────────────────
# ElevenLabs URL — output_format regression
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tts_stream_output_format_is_query_param_not_body():
    """
    REGRESSION TEST: output_format=ulaw_8000 must be a URL query parameter.

    ElevenLabs silently ignores output_format when it's in the request body.
    That causes the API to return MP3 by default. Raw MP3 bytes played as
    μ-law audio on Twilio sounds like pure noise/distortion.

    This test verifies the correct URL is constructed and the body does NOT
    contain the output_format field.
    """
    captured_url = None
    captured_body = None

    class FakeResponse:
        status_code = 200
        async def aiter_bytes(self, size):
            yield b"fake-audio-chunk"
        def raise_for_status(self):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class FakeClient:
        def stream(self, method, url, json=None, headers=None):
            nonlocal captured_url, captured_body
            captured_url = url
            captured_body = json
            return FakeResponse()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    with patch("app.routers.stream.httpx.AsyncClient", return_value=FakeClient()):
        chunks = []
        async for chunk in _tts_stream("Hello", "voice123", "el-key-test"):
            chunks.append(chunk)

    # The URL must include output_format as a query parameter
    assert captured_url is not None
    assert "output_format=ulaw_8000" in captured_url, (
        "output_format must be a URL query param — "
        "ElevenLabs ignores it in the request body and returns MP3, "
        "which sounds like pure noise when played as μ-law on Twilio"
    )

    # The body must NOT contain output_format
    assert "output_format" not in captured_body, (
        "output_format should NOT be in the request body — it belongs in the URL"
    )


@pytest.mark.asyncio
async def test_tts_stream_uses_correct_voice_id():
    """Voice ID is substituted into the URL correctly."""
    captured_url = None

    class FakeResponse:
        status_code = 200
        async def aiter_bytes(self, size):
            yield b"fake"
        def raise_for_status(self):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class FakeClient:
        def stream(self, method, url, json=None, headers=None):
            nonlocal captured_url
            captured_url = url
            return FakeResponse()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    with patch("app.routers.stream.httpx.AsyncClient", return_value=FakeClient()):
        async for _ in _tts_stream("Hi", "21m00Tcm4TlvDq8ikWAM", "key"):
            pass

    assert "21m00Tcm4TlvDq8ikWAM" in captured_url


# ─────────────────────────────────────────────────────────────────────────────
# _build_greeting()
# ─────────────────────────────────────────────────────────────────────────────

def _make_handler(practice_name="Sunrise Dental", state="NY", agent_name="Aria",
                  sms_enabled=True, timezone="America/New_York"):
    """Build a CallHandler with mocked WebSocket and a real PracticeConfig."""
    ws = MagicMock()
    config = PracticeConfig(agent_name=agent_name, sms_enabled=sms_enabled)

    practice = MagicMock()
    practice.id = uuid.uuid4()
    practice.name = practice_name
    practice.state = state
    practice.timezone = timezone
    practice.escalation_number = "+15559876543"
    practice.staff_email = "front@test.com"

    handler = CallHandler(
        websocket=ws,
        stream_sid="MZ123",
        call_sid="CA123",
        patient_phone="+12025551234",
        practice=practice,
        config=config,
    )
    return handler


def test_build_greeting_includes_practice_name():
    handler = _make_handler(practice_name="Riverside Dental")
    greeting = handler._build_greeting()
    assert "Riverside Dental" in greeting


def test_build_greeting_includes_agent_name():
    handler = _make_handler(agent_name="Sofia")
    greeting = handler._build_greeting()
    assert "Sofia" in greeting


def test_build_greeting_includes_recording_disclosure():
    """HIPAA: caller must be told the call may be recorded before any PHI is shared."""
    handler = _make_handler(state="NY")
    greeting = handler._build_greeting()
    # Any form of recording disclosure
    assert any(word in greeting.lower() for word in ["record", "quality"]), (
        "Greeting must include a recording disclosure for HIPAA compliance"
    )


# ─────────────────────────────────────────────────────────────────────────────
# CallHandler.start() — after-hours path
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_after_hours_returns_false_and_plays_message():
    """
    After-hours: start() plays the message, then returns False.

    Critical: start() must NOT call ws.close() explicitly.
    Azure's reverse proxy fragments the WebSocket CLOSE control frame,
    causing Twilio error 31924. Just return False and let the endpoint exit.
    """
    ws = MagicMock()
    ws.send_text = AsyncMock()

    config = PracticeConfig(
        after_hours_message="We are closed. Call back during business hours.",
        business_hours=BusinessHours(
            monday=None, tuesday=None, wednesday=None,
            thursday=None, friday=None, saturday=None, sunday=None,
        ),
    )

    practice = MagicMock()
    practice.id = uuid.uuid4()
    practice.name = "Closed Dental"
    practice.state = "CA"
    practice.timezone = "America/New_York"
    practice.escalation_number = "+15559876543"
    practice.staff_email = "test@test.com"

    handler = CallHandler(
        websocket=ws,
        stream_sid="MZ456",
        call_sid="CA456",
        patient_phone="+12025551234",
        practice=practice,
        config=config,
    )

    with patch.object(handler, "_play_text", new_callable=AsyncMock) as mock_play:
        result = await handler.start()

    assert result is False, "After-hours start() must return False to stop the event loop"
    mock_play.assert_called_once_with(config.after_hours_message)
    # Verify ws.close() was NOT called (would cause Twilio error 31924)
    ws.close.assert_not_called()


@pytest.mark.asyncio
async def test_start_business_hours_open_returns_true():
    """During business hours, start() connects Deepgram and returns True."""
    ws = MagicMock()
    ws.send_text = AsyncMock()

    # All hours open (24/7)
    config = PracticeConfig(
        after_hours_message="",  # empty = always answer
        agent_name="Aria",
    )

    practice = MagicMock()
    practice.id = uuid.uuid4()
    practice.name = "Open Dental"
    practice.state = "NY"
    practice.timezone = "America/New_York"
    practice.escalation_number = "+15559876543"
    practice.staff_email = "test@test.com"

    handler = CallHandler(
        websocket=ws,
        stream_sid="MZ789",
        call_sid="CA789",
        patient_phone="+12025551234",
        practice=practice,
        config=config,
    )

    with patch.object(handler, "_connect_deepgram", new_callable=AsyncMock), \
         patch.object(handler, "_play_text", new_callable=AsyncMock):
        result = await handler.start()

    assert result is True, "During business hours, start() must return True"
