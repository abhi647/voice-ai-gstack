"""
Tests for patient-facing booking confirmation SMS.

Coverage:
  NotifyAdapter.send_booking_confirmation_sms()
    ├── [✓] sends SMS to patient_phone (not escalation_number)
    ├── [✓] includes practice name in message body
    ├── [✓] includes patient first name when available
    ├── [✓] includes service type when available
    ├── [✓] includes requested time when available
    ├── [✓] skips when Twilio not configured
    ├── [✓] skips when twilio_sms_from not configured
    ├── [✓] skips when patient_phone is empty
    └── [✓] Twilio exception is non-raising, returns False

  POST /internal/finalize_call — sms_sent_at tracking
    ├── [✓] sms_sent_at set on call when patient SMS succeeds
    └── [✓] sms_sent_at NOT set when Twilio not configured (returns None)
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.ehr.base import BookingRequest
from app.ehr.notify import NotifyAdapter
from app.main import app


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


class TestSendBookingConfirmationSms:
    """Unit tests for NotifyAdapter.send_booking_confirmation_sms."""

    @pytest.mark.asyncio
    @patch("app.ehr.notify.settings")
    async def test_sends_to_patient_not_staff(self, mock_settings):
        """SMS must go to patient_phone, not escalation_number."""
        mock_settings.twilio_account_sid = "ACtest"
        mock_settings.twilio_auth_token = "token"
        mock_settings.twilio_sms_from = "+15550001111"

        captured_to = []

        with patch("twilio.rest.Client") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.side_effect = lambda to, from_, body: captured_to.append(to)

            adapter = NotifyAdapter()
            req = _make_booking(patient_phone="+12025551234", escalation_number="+15559876543")
            result = await adapter.send_booking_confirmation_sms(req)

        assert result is True
        assert captured_to == ["+12025551234"], "SMS must be sent to patient, not practice staff"

    @pytest.mark.asyncio
    @patch("app.ehr.notify.settings")
    async def test_includes_practice_name(self, mock_settings):
        mock_settings.twilio_account_sid = "ACtest"
        mock_settings.twilio_auth_token = "token"
        mock_settings.twilio_sms_from = "+15550001111"

        captured_body = []

        with patch("twilio.rest.Client") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.side_effect = lambda to, from_, body: captured_body.append(body)

            adapter = NotifyAdapter()
            await adapter.send_booking_confirmation_sms(_make_booking(practice_name="Riverside Dental"))

        assert "Riverside Dental" in captured_body[0]

    @pytest.mark.asyncio
    @patch("app.ehr.notify.settings")
    async def test_includes_patient_first_name(self, mock_settings):
        mock_settings.twilio_account_sid = "ACtest"
        mock_settings.twilio_auth_token = "token"
        mock_settings.twilio_sms_from = "+15550001111"

        captured_body = []

        with patch("twilio.rest.Client") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.side_effect = lambda to, from_, body: captured_body.append(body)

            adapter = NotifyAdapter()
            await adapter.send_booking_confirmation_sms(_make_booking(patient_name="Maria Torres"))

        assert "Maria" in captured_body[0]

    @pytest.mark.asyncio
    @patch("app.ehr.notify.settings")
    async def test_includes_service_and_time(self, mock_settings):
        mock_settings.twilio_account_sid = "ACtest"
        mock_settings.twilio_auth_token = "token"
        mock_settings.twilio_sms_from = "+15550001111"

        captured_body = []

        with patch("twilio.rest.Client") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.side_effect = lambda to, from_, body: captured_body.append(body)

            adapter = NotifyAdapter()
            await adapter.send_booking_confirmation_sms(
                _make_booking(service_type="root canal", requested_time="Wednesday 3pm")
            )

        body = captured_body[0]
        assert "root canal" in body
        assert "Wednesday 3pm" in body

    @pytest.mark.asyncio
    @patch("app.ehr.notify.settings")
    async def test_skips_when_twilio_not_configured(self, mock_settings):
        mock_settings.twilio_account_sid = ""
        mock_settings.twilio_auth_token = ""

        adapter = NotifyAdapter()
        result = await adapter.send_booking_confirmation_sms(_make_booking())
        assert result is False

    @pytest.mark.asyncio
    @patch("app.ehr.notify.settings")
    async def test_skips_when_no_sms_from(self, mock_settings):
        mock_settings.twilio_account_sid = "ACtest"
        mock_settings.twilio_auth_token = "token"
        mock_settings.twilio_sms_from = ""

        adapter = NotifyAdapter()
        result = await adapter.send_booking_confirmation_sms(_make_booking())
        assert result is False

    @pytest.mark.asyncio
    async def test_skips_when_no_patient_phone(self):
        adapter = NotifyAdapter()
        result = await adapter.send_booking_confirmation_sms(_make_booking(patient_phone=""))
        assert result is False

    @pytest.mark.asyncio
    @patch("app.ehr.notify.settings")
    async def test_twilio_exception_is_non_raising(self, mock_settings):
        mock_settings.twilio_account_sid = "ACtest"
        mock_settings.twilio_auth_token = "token"
        mock_settings.twilio_sms_from = "+15550001111"

        with patch("twilio.rest.Client") as mock_cls:
            mock_cls.return_value.messages.create.side_effect = Exception("network error")
            adapter = NotifyAdapter()
            result = await adapter.send_booking_confirmation_sms(_make_booking())

        assert result is False, "Twilio errors must be caught and return False"


# ---------------------------------------------------------------------------
# finalize_call — sms_sent_at tracking
# ---------------------------------------------------------------------------


def _finalize_payload(**kwargs) -> dict:
    return {
        "call_sid": kwargs.get("call_sid", f"CA{uuid.uuid4().hex[:10]}"),
        "practice_id": kwargs.get("practice_id", "test-practice"),
        "patient_phone": kwargs.get("patient_phone", "+15550000000"),
        "started_at": kwargs.get("started_at", "2026-03-31T10:00:00Z"),
        "disposition": kwargs.get("disposition", "BOOKING_CAPTURED"),
        "patient_name": kwargs.get("patient_name", "Jane Smith"),
        "requested_time": kwargs.get("requested_time", "Tuesday 10am"),
        "service_type": kwargs.get("service_type", "cleaning"),
        "summary": kwargs.get("summary", "Patient wants a cleaning."),
    }


def _make_db(existing_call=None):
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=existing_call)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()
    return db


class TestFinalizeCallSmsTracking:
    def setup_method(self):
        app.dependency_overrides = {}

    def teardown_method(self):
        app.dependency_overrides = {}

    def _override_db(self, db):
        async def _get_db_override():
            yield db
        app.dependency_overrides[get_db] = _get_db_override

    @patch("app.routers.internal.audit_log", new_callable=AsyncMock)
    @patch("app.routers.internal.s3.upload_transcript")
    @patch("app.ehr.notify.NotifyAdapter.send_booking_confirmation_sms", new_callable=AsyncMock)
    def test_sms_sent_at_set_when_patient_sms_succeeds(
        self, mock_patient_sms, mock_upload, mock_audit
    ):
        """When patient SMS succeeds, sms_sent_at is written on the call record."""
        mock_patient_sms.return_value = True
        mock_upload.return_value = "some/key"

        db = _make_db(existing_call=None)
        self._override_db(db)

        call_sid = f"CA{uuid.uuid4().hex[:10]}"
        with TestClient(app) as client:
            resp = client.post("/internal/finalize_call", json=_finalize_payload(call_sid=call_sid))

        assert resp.status_code == 200
        call_record = db.add.call_args[0][0]
        assert call_record.sms_sent_at is not None, (
            "sms_sent_at must be set when the patient confirmation SMS succeeds"
        )

    @patch("app.routers.internal.audit_log", new_callable=AsyncMock)
    @patch("app.routers.internal.s3.upload_transcript")
    @patch("app.ehr.notify.NotifyAdapter.send_booking_confirmation_sms", new_callable=AsyncMock)
    def test_sms_sent_at_null_when_patient_sms_fails(
        self, mock_patient_sms, mock_upload, mock_audit
    ):
        """When patient SMS fails (e.g. Twilio not configured), sms_sent_at stays NULL."""
        mock_patient_sms.return_value = False
        mock_upload.return_value = "some/key"

        db = _make_db(existing_call=None)
        self._override_db(db)

        call_sid = f"CA{uuid.uuid4().hex[:10]}"
        with TestClient(app) as client:
            resp = client.post("/internal/finalize_call", json=_finalize_payload(call_sid=call_sid))

        assert resp.status_code == 200
        call_record = db.add.call_args[0][0]
        assert call_record.sms_sent_at is None, (
            "sms_sent_at must stay NULL when the patient confirmation SMS was not sent"
        )
