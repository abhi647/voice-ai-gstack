"""
Tests for EHR adapters (app/ehr/).

Coverage:
  NotifyAdapter
    ├── [✓] success — both SMS and email succeed
    ├── [✓] SMS only — no staff_email configured
    ├── [✓] email only — Twilio SMS not configured
    ├── [✓] both fail — returns success=False
    ├── [✓] SMS Twilio error — non-raising, returns False
    ├── [✓] email SendGrid error — non-raising, returns False
    └── [✓] SMS body includes patient name, service, phone, call SID

  get_ehr_adapter (factory)
    ├── [✓] "notify" returns NotifyAdapter
    ├── [✓] planned adapter ("dentrix") falls back to NotifyAdapter with warning
    ├── [✓] unknown adapter falls back to NotifyAdapter with error log
    └── [✓] case-insensitive ("Notify" == "notify")
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.ehr.base import BookingRequest
from app.ehr.factory import get_ehr_adapter
from app.ehr.notify import NotifyAdapter, _sms_body


def _make_booking(**kwargs) -> BookingRequest:
    return BookingRequest(
        practice_id=kwargs.get("practice_id", "test-practice"),
        practice_name=kwargs.get("practice_name", "Sunrise Dental"),
        practice_timezone=kwargs.get("practice_timezone", "America/New_York"),
        escalation_number=kwargs.get("escalation_number", "+15559876543"),
        staff_email=kwargs.get("staff_email", "front@sunrise.com"),
        patient_name=kwargs.get("patient_name", "Jane Smith"),
        patient_phone=kwargs.get("patient_phone", "+15550000000"),
        service_type=kwargs.get("service_type", "cleaning"),
        requested_time=kwargs.get("requested_time", "Tuesday afternoon"),
        notes=kwargs.get("notes", None),
        call_sid=kwargs.get("call_sid", "CA123"),
    )


class TestNotifyAdapter:
    @pytest.mark.asyncio
    @patch("app.ehr.notify.settings")
    async def test_both_sms_and_email_succeed(self, mock_settings):
        mock_settings.twilio_account_sid = "ACtest"
        mock_settings.twilio_auth_token = "token"
        mock_settings.twilio_sms_from = "+15550001111"
        mock_settings.sendgrid_api_key = "SG.test"
        mock_settings.sendgrid_from_email = "noreply@test.com"

        adapter = NotifyAdapter()
        with patch.object(adapter, "_send_sms", new=AsyncMock(return_value=True)), \
             patch.object(adapter, "_send_email", new=AsyncMock(return_value=True)):
            result = await adapter.submit_booking(_make_booking())

        assert result.success is True
        assert "SMS" in result.message
        assert "email" in result.message

    @pytest.mark.asyncio
    async def test_sms_only_when_no_staff_email(self):
        adapter = NotifyAdapter()
        with patch.object(adapter, "_send_sms", new=AsyncMock(return_value=True)), \
             patch.object(adapter, "_send_email", new=AsyncMock(return_value=False)):
            result = await adapter.submit_booking(_make_booking(staff_email=None))

        assert result.success is True
        assert "SMS" in result.message

    @pytest.mark.asyncio
    async def test_email_only_when_sms_not_configured(self):
        adapter = NotifyAdapter()
        with patch.object(adapter, "_send_sms", new=AsyncMock(return_value=False)), \
             patch.object(adapter, "_send_email", new=AsyncMock(return_value=True)):
            result = await adapter.submit_booking(_make_booking())

        assert result.success is True
        assert "email" in result.message

    @pytest.mark.asyncio
    async def test_both_fail_returns_failure(self):
        adapter = NotifyAdapter()
        with patch.object(adapter, "_send_sms", new=AsyncMock(return_value=False)), \
             patch.object(adapter, "_send_email", new=AsyncMock(return_value=False)):
            result = await adapter.submit_booking(_make_booking())

        assert result.success is False

    @pytest.mark.asyncio
    @patch("app.ehr.notify.settings")
    async def test_twilio_exception_returns_false(self, mock_settings):
        mock_settings.twilio_account_sid = "ACtest"
        mock_settings.twilio_auth_token = "token"
        mock_settings.twilio_sms_from = "+15550001111"

        adapter = NotifyAdapter()
        with patch("twilio.rest.Client") as mock_client_cls:
            mock_client_cls.return_value.messages.create.side_effect = Exception("Twilio error")
            result = await adapter._send_sms(_make_booking())

        assert result is False

    @pytest.mark.asyncio
    @patch("app.ehr.notify.settings")
    async def test_sendgrid_exception_returns_false(self, mock_settings):
        mock_settings.sendgrid_api_key = "SG.test"
        mock_settings.sendgrid_from_email = "noreply@test.com"

        adapter = NotifyAdapter()
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(
                    post=AsyncMock(side_effect=Exception("SendGrid timeout"))
                )
            )
            result = await adapter._send_email(_make_booking())

        assert result is False

    def test_sms_body_includes_key_fields(self):
        req = _make_booking(
            patient_name="Jane Smith",
            service_type="cleaning",
            requested_time="Tuesday afternoon",
            patient_phone="+15550000000",
            call_sid="CA999",
        )
        body = _sms_body(req)
        assert "Jane Smith" in body
        assert "cleaning" in body
        assert "Tuesday afternoon" in body
        assert "+15550000000" in body
        assert "CA999" in body

    @pytest.mark.asyncio
    @patch("app.ehr.notify.settings")
    async def test_skips_sms_when_no_twilio_sms_from(self, mock_settings):
        mock_settings.twilio_account_sid = "ACtest"
        mock_settings.twilio_auth_token = "token"
        mock_settings.twilio_sms_from = ""  # not configured

        adapter = NotifyAdapter()
        result = await adapter._send_sms(_make_booking())
        assert result is False


class TestEHRFactory:
    def test_notify_returns_notify_adapter(self):
        adapter = get_ehr_adapter("notify")
        assert isinstance(adapter, NotifyAdapter)

    def test_planned_adapter_falls_back_to_notify(self):
        adapter = get_ehr_adapter("dentrix")
        assert isinstance(adapter, NotifyAdapter)

    def test_unknown_adapter_falls_back_to_notify(self):
        adapter = get_ehr_adapter("not_a_real_ehr")
        assert isinstance(adapter, NotifyAdapter)

    def test_case_insensitive(self):
        adapter = get_ehr_adapter("Notify")
        assert isinstance(adapter, NotifyAdapter)
