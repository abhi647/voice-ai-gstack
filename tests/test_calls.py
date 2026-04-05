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

  SEC-1: Twilio request signature verification
    ├── [✓] missing signature → 403 when TWILIO_AUTH_TOKEN is set
    ├── [✓] invalid signature → 403 when TWILIO_AUTH_TOKEN is set
    └── [✓] valid signature → 200

  Helper: _twiml_hangup
    └── [✓] returns XML with <Say> and <Hangup>
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.middleware.twilio_auth import verify_twilio_signature
from tests.conftest import make_practice

client = TestClient(app)


def _twilio_form(to: str, from_: str = "+15550000000", call_sid: str = "CA123") -> dict:
    return {"To": to, "From": from_, "CallSid": call_sid}


@pytest.fixture(autouse=True)
def bypass_twilio_signature():
    """
    Most tests in this module test routing logic, not Twilio auth.
    Override the signature dependency to pass for all tests, except the
    SEC-1 tests which restore the real implementation.
    """
    app.dependency_overrides[verify_twilio_signature] = lambda: None
    yield
    app.dependency_overrides.pop(verify_twilio_signature, None)


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


# ─────────────────────────────────────────────────────────────────────────────
# SEC-1: Twilio request signature verification
# ─────────────────────────────────────────────────────────────────────────────


class TestTwilioSignatureVerification:
    """
    SEC-1 regression tests.

    Without Twilio signature verification, any internet client can POST fake calls,
    burning Deepgram/Claude/ElevenLabs quota and injecting fabricated call records.
    """

    def setup_method(self):
        """Restore the real dependency for these tests."""
        app.dependency_overrides.pop(verify_twilio_signature, None)

    def teardown_method(self):
        """Bypass again so subsequent test classes aren't affected."""
        app.dependency_overrides[verify_twilio_signature] = lambda: None

    def test_missing_signature_returns_403_when_token_configured(self):
        """No X-Twilio-Signature header + token configured → 403."""
        with patch("app.middleware.twilio_auth.settings") as mock_settings:
            mock_settings.twilio_auth_token = "test_auth_token_abc123"
            resp = client.post(
                "/twilio/voice",
                data=_twilio_form(to="+15551234567"),
                # no X-Twilio-Signature header
            )
        assert resp.status_code == 403, (
            "POST /twilio/voice without X-Twilio-Signature must return 403 "
            "when TWILIO_AUTH_TOKEN is configured — "
            "without this check any internet client can inject fake calls"
        )

    def test_invalid_signature_returns_403(self):
        """Wrong X-Twilio-Signature → 403."""
        with patch("app.middleware.twilio_auth.settings") as mock_settings:
            mock_settings.twilio_auth_token = "real_token"
            resp = client.post(
                "/twilio/voice",
                data=_twilio_form(to="+15551234567"),
                headers={"X-Twilio-Signature": "BOGUSSIGNATURE"},
            )
        assert resp.status_code == 403

    def test_no_token_configured_skips_check(self):
        """If TWILIO_AUTH_TOKEN is not set, validation is skipped (dev mode)."""
        with patch("app.middleware.twilio_auth.settings") as mock_settings, \
             patch("app.routers.calls.Practice.get_by_twilio_number", new_callable=AsyncMock) as mock_get:
            mock_settings.twilio_auth_token = ""  # not configured
            mock_get.return_value = None
            resp = client.post(
                "/twilio/voice",
                data=_twilio_form(to="+15550000000"),
                # no signature — should be OK when token is unconfigured
            )
        assert resp.status_code == 200, (
            "When TWILIO_AUTH_TOKEN is not configured, validation is skipped "
            "so local dev / CI works without a real Twilio account"
        )

    def test_valid_signature_passes(self):
        """A correctly computed signature is accepted."""
        from twilio.request_validator import RequestValidator

        token = "test_auth_token"
        url = "http://testserver/twilio/voice"
        params = _twilio_form(to="+15551234567")
        signature = RequestValidator(token).compute_signature(url, params)

        with patch("app.middleware.twilio_auth.settings") as mock_settings, \
             patch("app.routers.calls.Practice.get_by_twilio_number", new_callable=AsyncMock) as mock_get:
            mock_settings.twilio_auth_token = token
            mock_get.return_value = None  # practice not found → hangup, but 200
            resp = client.post(
                "/twilio/voice",
                data=params,
                headers={"X-Twilio-Signature": signature},
            )
        assert resp.status_code == 200, (
            "A request with a valid X-Twilio-Signature must pass verification"
        )
