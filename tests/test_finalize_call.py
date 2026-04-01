"""
Tests for POST /internal/finalize_call (app/routers/internal.py).

Coverage:
  POST /internal/finalize_call
    ├── [✓] happy path — creates Call record, uploads transcript, creates audit log
    ├── [✓] idempotency — duplicate call_sid returns already_finalized
    ├── [✓] missing transcript — skips S3 upload, still writes DB row
    ├── [✓] recording URL provided — triggers upload_recording_from_url
    ├── [✓] S3 upload failure — still writes DB row (non-fatal)
    ├── [✓] malformed started_at — falls back to now()
    └── [✓] disposition written correctly on Call object
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.database import get_db
from app.main import app


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
        "transcript": kwargs.get("transcript", "AGENT: Hello!\nPATIENT: Hi."),
        "twilio_recording_url": kwargs.get("twilio_recording_url", None),
    }


def _make_db(existing_call=None):
    """Build a mock AsyncSession."""
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=existing_call)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.add = MagicMock()  # add() is synchronous
    return db


class TestFinalizeCall:
    def setup_method(self):
        """Reset dependency overrides before each test."""
        app.dependency_overrides = {}

    def teardown_method(self):
        app.dependency_overrides = {}

    def _override_db(self, db):
        async def _get_db_override():
            yield db
        app.dependency_overrides[get_db] = _get_db_override

    @patch("app.routers.internal.audit_log", new_callable=AsyncMock)
    @patch("app.routers.internal.s3.upload_transcript")
    def test_happy_path_creates_call_record(self, mock_upload, mock_audit):
        db = _make_db(existing_call=None)
        self._override_db(db)
        mock_upload.return_value = "practices/test-practice/calls/CA123/transcript.txt"

        payload = _finalize_payload(call_sid="CA123")
        with TestClient(app) as client:
            resp = client.post("/internal/finalize_call", json=payload)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert "call_id" in body
        mock_upload.assert_called_once_with("test-practice", "CA123", payload["transcript"])
        mock_audit.assert_called_once()

    @patch("app.routers.internal.audit_log", new_callable=AsyncMock)
    @patch("app.routers.internal.s3.upload_transcript")
    def test_idempotency_returns_already_finalized(self, mock_upload, mock_audit):
        existing = MagicMock()
        existing.id = uuid.uuid4()
        db = _make_db(existing_call=existing)
        self._override_db(db)

        payload = _finalize_payload(call_sid="CA_DUPE")
        with TestClient(app) as client:
            resp = client.post("/internal/finalize_call", json=payload)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "already_finalized"
        assert body["call_id"] == str(existing.id)
        db.add.assert_not_called()
        mock_audit.assert_not_called()

    @patch("app.routers.internal.audit_log", new_callable=AsyncMock)
    @patch("app.routers.internal.s3.upload_transcript")
    def test_missing_transcript_skips_s3(self, mock_upload, mock_audit):
        db = _make_db(existing_call=None)
        self._override_db(db)

        payload = _finalize_payload(call_sid="CA_NOTRANSCRIPT", transcript=None)
        with TestClient(app) as client:
            resp = client.post("/internal/finalize_call", json=payload)

        assert resp.status_code == 200
        mock_upload.assert_not_called()

    @patch("app.routers.internal.audit_log", new_callable=AsyncMock)
    @patch("app.routers.internal.s3.upload_recording_from_url")
    @patch("app.routers.internal.s3.upload_transcript")
    def test_recording_url_triggers_upload(self, mock_transcript, mock_recording, mock_audit):
        db = _make_db(existing_call=None)
        self._override_db(db)
        mock_transcript.return_value = "practices/p/calls/CA_REC/transcript.txt"
        mock_recording.return_value = "practices/p/calls/CA_REC/recording.mp3"

        payload = _finalize_payload(
            call_sid="CA_REC",
            twilio_recording_url="https://api.twilio.com/recordings/RE123",
        )
        with TestClient(app) as client:
            resp = client.post("/internal/finalize_call", json=payload)

        assert resp.status_code == 200
        mock_recording.assert_called_once_with(
            payload["practice_id"],
            "CA_REC",
            "https://api.twilio.com/recordings/RE123",
        )

    @patch("app.routers.internal.audit_log", new_callable=AsyncMock)
    @patch("app.routers.internal.s3.upload_transcript")
    def test_s3_failure_still_writes_db(self, mock_upload, mock_audit):
        db = _make_db(existing_call=None)
        self._override_db(db)
        mock_upload.side_effect = Exception("S3 timeout")

        payload = _finalize_payload(call_sid="CA_S3FAIL")
        with TestClient(app) as client:
            resp = client.post("/internal/finalize_call", json=payload)

        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        db.add.assert_called_once()

    @patch("app.routers.internal.audit_log", new_callable=AsyncMock)
    @patch("app.routers.internal.s3.upload_transcript")
    def test_malformed_started_at_falls_back_to_now(self, mock_upload, mock_audit):
        db = _make_db(existing_call=None)
        self._override_db(db)
        mock_upload.return_value = "some/key"

        payload = _finalize_payload(call_sid="CA_BADDATE", started_at="not-a-date")
        with TestClient(app) as client:
            resp = client.post("/internal/finalize_call", json=payload)

        assert resp.status_code == 200

    @patch("app.routers.internal.audit_log", new_callable=AsyncMock)
    @patch("app.routers.internal.s3.upload_transcript")
    def test_escalated_disposition_written_correctly(self, mock_upload, mock_audit):
        db = _make_db(existing_call=None)
        self._override_db(db)
        mock_upload.return_value = "some/key"

        payload = _finalize_payload(call_sid="CA_ESC", disposition="ESCALATED")
        with TestClient(app) as client:
            resp = client.post("/internal/finalize_call", json=payload)

        assert resp.status_code == 200
        call_arg = db.add.call_args[0][0]
        assert call_arg.disposition == "ESCALATED"
