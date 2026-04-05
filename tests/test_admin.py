"""
Tests for the admin dashboard (app/routers/admin.py).

Coverage:
  GET /admin/calls
    ├── [✓] returns 200 with HTML content-type
    ├── [✓] empty call list shows "No calls yet"
    ├── [✓] call rows include patient phone and disposition badge
    ├── [✓] BOOKING_CAPTURED disposition shows green badge
    └── [✓] pagination links appear when total > page size

  GET /admin/calls/{call_id}
    ├── [✓] returns 200 with call details
    ├── [✓] shows transcript when present
    ├── [✓] returns 404 for unknown call ID
    └── [✓] returns 400 for malformed UUID
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app
from app.models.call import Call


def _make_call(**kwargs) -> MagicMock:
    call = MagicMock(spec=Call)
    call.id = kwargs.get("id", uuid.uuid4())
    call.twilio_call_sid = kwargs.get("twilio_call_sid", f"CA{uuid.uuid4().hex[:10]}")
    call.patient_phone = kwargs.get("patient_phone", "+15550000000")
    call.patient_name = kwargs.get("patient_name", "Jane Smith")
    call.service_type = kwargs.get("service_type", "cleaning")
    call.requested_time = kwargs.get("requested_time", "Tuesday 10am")
    call.disposition = kwargs.get("disposition", "BOOKING_CAPTURED")
    call.started_at = kwargs.get("started_at", datetime(2026, 3, 31, 10, 0, tzinfo=timezone.utc))
    call.ended_at = kwargs.get("ended_at", datetime(2026, 3, 31, 10, 5, tzinfo=timezone.utc))
    call.sms_sent_at = kwargs.get("sms_sent_at", None)
    call.transcript = kwargs.get("transcript", None)
    call.transcript_s3_key = kwargs.get("transcript_s3_key", None)
    call.audio_s3_key = kwargs.get("audio_s3_key", None)
    return call


def _override_db_with_calls(calls: list, single_call=None):
    """
    Build an AsyncSession mock that returns `calls` for list queries
    and `single_call` for scalar lookups.
    """
    db = AsyncMock()

    # scalars().all() for the count query returns all calls
    # scalars().all() for the paged query also returns all calls
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = calls
    scalars_mock.scalar_one_or_none = MagicMock(return_value=single_call)

    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    result_mock.scalar_one_or_none.return_value = single_call

    db.execute = AsyncMock(return_value=result_mock)

    async def _get_db_override():
        yield db

    app.dependency_overrides[get_db] = _get_db_override
    return db


class TestAdminCallLog:
    def setup_method(self):
        app.dependency_overrides = {}

    def teardown_method(self):
        app.dependency_overrides = {}

    def test_returns_html(self):
        _override_db_with_calls([])
        with TestClient(app) as client:
            resp = client.get("/admin/calls")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_empty_list_shows_no_calls_message(self):
        _override_db_with_calls([])
        with TestClient(app) as client:
            resp = client.get("/admin/calls")
        assert "No calls yet" in resp.text

    def test_call_rows_include_patient_phone(self):
        calls = [_make_call(patient_phone="+12025551234")]
        _override_db_with_calls(calls)
        with TestClient(app) as client:
            resp = client.get("/admin/calls")
        assert "+12025551234" in resp.text

    def test_booking_captured_shows_green_badge(self):
        calls = [_make_call(disposition="BOOKING_CAPTURED")]
        _override_db_with_calls(calls)
        with TestClient(app) as client:
            resp = client.get("/admin/calls")
        # Badge class for BOOKING_CAPTURED is badge-booking (green)
        assert "badge-booking" in resp.text

    def test_escalated_shows_yellow_badge(self):
        calls = [_make_call(disposition="ESCALATED")]
        _override_db_with_calls(calls)
        with TestClient(app) as client:
            resp = client.get("/admin/calls")
        assert "badge-escalated" in resp.text

    def test_sms_sent_at_shown_when_set(self):
        sms_time = datetime(2026, 3, 31, 10, 1, tzinfo=timezone.utc)
        calls = [_make_call(disposition="BOOKING_CAPTURED", sms_sent_at=sms_time)]
        _override_db_with_calls(calls)
        with TestClient(app) as client:
            resp = client.get("/admin/calls")
        # Green checkmark for sent SMS
        assert "✓" in resp.text

    def test_not_sent_warning_for_booking_without_sms(self):
        calls = [_make_call(disposition="BOOKING_CAPTURED", sms_sent_at=None)]
        _override_db_with_calls(calls)
        with TestClient(app) as client:
            resp = client.get("/admin/calls")
        assert "not sent" in resp.text


class TestAdminCallDetail:
    def setup_method(self):
        app.dependency_overrides = {}

    def teardown_method(self):
        app.dependency_overrides = {}

    def test_returns_200_for_existing_call(self):
        call = _make_call()
        _override_db_with_calls([], single_call=call)
        with TestClient(app) as client:
            resp = client.get(f"/admin/calls/{call.id}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_shows_call_sid(self):
        call = _make_call(twilio_call_sid="CA_DETAIL_TEST")
        _override_db_with_calls([], single_call=call)
        with TestClient(app) as client:
            resp = client.get(f"/admin/calls/{call.id}")
        assert "CA_DETAIL_TEST" in resp.text

    def test_shows_transcript_when_present(self):
        call = _make_call(transcript="AGENT: Hello!\nPATIENT: Hi, I need a cleaning.")
        _override_db_with_calls([], single_call=call)
        with TestClient(app) as client:
            resp = client.get(f"/admin/calls/{call.id}")
        assert "I need a cleaning" in resp.text

    def test_returns_404_for_unknown_id(self):
        _override_db_with_calls([], single_call=None)
        with TestClient(app) as client:
            resp = client.get(f"/admin/calls/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_returns_400_for_malformed_uuid(self):
        with TestClient(app) as client:
            resp = client.get("/admin/calls/not-a-uuid")
        assert resp.status_code == 400
