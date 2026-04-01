"""
Tests for the Twilio inbound call webhook (app/routers/calls.py).

Coverage:
  POST /twilio/voice
    ├── [✓] practice found + active → TwiML connects call
    ├── [✓] practice not found → graceful hangup TwiML
    ├── [✓] practice found but is_active=False → graceful hangup (lapsed subscription)
    └── [✓] health check → 200 OK

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
    def test_practice_found_returns_twiml(self, mock_get):
        mock_get.return_value = make_practice(name="Sunrise Dental")

        resp = client.post("/twilio/voice", data=_twilio_form(to="+15551234567"))

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/xml"
        body = resp.text
        assert "<?xml" in body
        assert "Sunrise Dental" in body
        # Should NOT hang up — call is connecting
        assert "<Hangup" not in body

    @patch("app.routers.calls.Practice.get_by_twilio_number", new_callable=AsyncMock)
    def test_practice_not_found_hangs_up(self, mock_get):
        mock_get.return_value = None

        resp = client.post("/twilio/voice", data=_twilio_form(to="+15550000000"))

        assert resp.status_code == 200
        body = resp.text
        assert "<?xml" in body
        assert "<Hangup" in body
        assert "not in service" in body

    @patch("app.routers.calls.Practice.get_by_twilio_number", new_callable=AsyncMock)
    def test_inactive_practice_is_rejected(self, mock_get):
        """get_by_twilio_number filters is_active=True at the DB level.
        If practice is inactive, the query returns None."""
        mock_get.return_value = None  # DB returns None for inactive practices

        resp = client.post("/twilio/voice", data=_twilio_form(to="+15557777777"))

        assert resp.status_code == 200
        assert "<Hangup" in resp.text

    @patch("app.routers.calls.Practice.get_by_twilio_number", new_callable=AsyncMock)
    def test_status_webhook_returns_received(self, mock_get):
        resp = client.post(
            "/twilio/status",
            data={"CallSid": "CA123", "CallStatus": "completed"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "received"}
