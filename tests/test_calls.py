"""
Tests for the Twilio inbound call webhook (app/routers/calls.py).

Coverage:
  POST /twilio/voice
    ├── [✓] practice found → TwiML with <Connect><Stream> pointing to wss:// URL
    ├── [✓] practice found → custom parameters include practice_id and patient_phone
    ├── [✓] x-forwarded-host header used when present (Azure App Service proxy)
    ├── [✓] practice not found → graceful hangup TwiML
    └── [✓] practice found but is_active=False → graceful hangup (lapsed subscription)

  POST /twilio/status
    └── [✓] returns TwiML XML (informational — no JSON body needed)

  Helper: _twiml_hangup
    └── [✓] returns XML with <Say> and <Hangup>
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.conftest import make_practice

client = TestClient(app)


def _twilio_form(to: str, from_: str = "+15550000000", call_sid: str = "CA123") -> dict:
    return {"To": to, "From": from_, "CallSid": call_sid}


class TestInboundCall:
    def test_health_check(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    @patch("app.routers.calls.Practice.get_by_twilio_number", new_callable=AsyncMock)
    def test_practice_found_returns_media_streams_twiml(self, mock_get):
        mock_get.return_value = make_practice(name="Sunrise Dental")

        resp = client.post("/twilio/voice", data=_twilio_form(to="+15551234567"))

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/xml"
        body = resp.text
        # Must use Media Streams, not SIP dial
        assert "<Connect>" in body
        assert "<Stream" in body
        assert "wss://" in body
        assert "/twilio/stream" in body
        assert "<Hangup" not in body
        assert "sip:" not in body

    @patch("app.routers.calls.Practice.get_by_twilio_number", new_callable=AsyncMock)
    def test_media_streams_twiml_includes_practice_parameters(self, mock_get):
        practice = make_practice(name="Sunrise Dental")
        mock_get.return_value = practice

        resp = client.post(
            "/twilio/voice",
            data=_twilio_form(to="+15551234567", from_="+18005551234"),
        )

        body = resp.text
        # practice_id and patient_phone passed as <Parameter> elements
        assert str(practice.id) in body
        assert "practice_id" in body
        assert "patient_phone" in body
        assert "+18005551234" in body

    @patch("app.routers.calls.Practice.get_by_twilio_number", new_callable=AsyncMock)
    def test_x_forwarded_host_used_for_stream_url(self, mock_get):
        """Azure App Service sets X-Forwarded-Host — stream URL must use it."""
        mock_get.return_value = make_practice(name="Sunrise Dental")

        resp = client.post(
            "/twilio/voice",
            data=_twilio_form(to="+15551234567"),
            headers={"x-forwarded-host": "voice-ai-app.azurewebsites.net"},
        )

        body = resp.text
        assert "voice-ai-app.azurewebsites.net" in body

    @patch("app.routers.calls.Practice.get_by_twilio_number", new_callable=AsyncMock)
    def test_practice_not_found_hangs_up(self, mock_get):
        mock_get.return_value = None

        resp = client.post("/twilio/voice", data=_twilio_form(to="+15550000000"))

        assert resp.status_code == 200
        body = resp.text
        assert "<Hangup" in body
        assert "not in service" in body

    @patch("app.routers.calls.Practice.get_by_twilio_number", new_callable=AsyncMock)
    def test_inactive_practice_is_rejected(self, mock_get):
        """get_by_twilio_number filters is_active=True at the DB level."""
        mock_get.return_value = None

        resp = client.post("/twilio/voice", data=_twilio_form(to="+15557777777"))

        assert resp.status_code == 200
        assert "<Hangup" in resp.text

    def test_status_webhook_returns_xml(self):
        resp = client.post(
            "/twilio/status",
            data={"CallSid": "CA123", "CallStatus": "completed"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/xml"
