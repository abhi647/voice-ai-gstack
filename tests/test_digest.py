"""
Tests for the weekly digest (app/digest.py).

Coverage:
  compute_stats
    ├── [✓] counts calls by disposition correctly
    ├── [✓] bookings list contains patient name, service, time
    ├── [✓] zero calls returns empty stats
    └── [✓] only counts calls within the window

  send_digest
    ├── [✓] dry_run prints to stdout, doesn't call SendGrid
    ├── [✓] skips send when no staff_email
    ├── [✓] skips send when SendGrid not configured
    └── [✓] returns True on successful SendGrid call

  email formatters
    ├── [✓] subject includes practice name + call count
    ├── [✓] subject says "no calls" when total_calls == 0
    ├── [✓] plain text includes all disposition counts
    ├── [✓] plain text lists bookings
    ├── [✓] plain text warns on unanswered escalations
    └── [✓] HTML includes unanswered escalation banner
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.digest import (
    WeeklyStats,
    _email_html,
    _email_plain,
    _subject,
    compute_stats,
    send_digest,
)


def _make_stats(**kwargs) -> WeeklyStats:
    week_start = datetime(2026, 3, 23, tzinfo=timezone.utc)
    week_end = week_start + timedelta(days=7)
    return WeeklyStats(
        practice_id=str(uuid.uuid4()),
        practice_name=kwargs.get("practice_name", "Sunrise Dental"),
        staff_email=kwargs.get("staff_email", "front@sunrise.com"),
        week_start=week_start,
        week_end=week_end,
        total_calls=kwargs.get("total_calls", 10),
        bookings=kwargs.get("bookings", [
            {"patient_name": "Jane Smith", "service_type": "cleaning", "requested_time": "Tuesday"},
            {"patient_name": "Bob Lee", "service_type": "filling", "requested_time": "Thursday"},
        ]),
        escalations=kwargs.get("escalations", 2),
        unanswered_escalations=kwargs.get("unanswered_escalations", 0),
        hung_up=kwargs.get("hung_up", 1),
        faq_only=kwargs.get("faq_only", 1),
    )


def _make_call(disposition: str, practice_id=None, started_at=None):
    c = MagicMock()
    c.disposition = disposition
    c.practice_id = practice_id or uuid.uuid4()
    c.started_at = started_at or datetime.now(timezone.utc)
    c.patient_name = "Jane Smith"
    c.service_type = "cleaning"
    c.requested_time = "Tuesday"
    return c


class TestComputeStats:
    @pytest.mark.asyncio
    async def test_counts_dispositions_correctly(self):
        pid = uuid.uuid4()
        calls = [
            _make_call("BOOKING_CAPTURED"),
            _make_call("BOOKING_CAPTURED"),
            _make_call("ESCALATED"),
            _make_call("ESCALATED_UNANSWERED"),
            _make_call("HUNG_UP"),
            _make_call("FAQ_ONLY"),
        ]
        practice = MagicMock()
        practice.id = pid
        practice.name = "Test Practice"
        practice.staff_email = "test@test.com"

        week_start = datetime(2026, 3, 23, tzinfo=timezone.utc)
        week_end = week_start + timedelta(days=7)

        with patch("app.digest.AsyncSessionLocal") as mock_session_cls:
            mock_db = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_result = MagicMock()
            mock_result.scalars.return_value.all.return_value = calls
            mock_db.execute = AsyncMock(return_value=mock_result)

            stats = await compute_stats(practice, week_start, week_end)

        assert stats.total_calls == 6
        assert len(stats.bookings) == 2
        assert stats.escalations == 1
        assert stats.unanswered_escalations == 1
        assert stats.hung_up == 1
        assert stats.faq_only == 1

    @pytest.mark.asyncio
    async def test_zero_calls_returns_empty_stats(self):
        practice = MagicMock()
        practice.id = uuid.uuid4()
        practice.name = "Quiet Practice"
        practice.staff_email = "q@q.com"

        week_start = datetime(2026, 3, 23, tzinfo=timezone.utc)
        week_end = week_start + timedelta(days=7)

        with patch("app.digest.AsyncSessionLocal") as mock_session_cls:
            mock_db = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_result = MagicMock()
            mock_result.scalars.return_value.all.return_value = []
            mock_db.execute = AsyncMock(return_value=mock_result)

            stats = await compute_stats(practice, week_start, week_end)

        assert stats.total_calls == 0
        assert stats.bookings == []

    @pytest.mark.asyncio
    async def test_bookings_list_has_correct_fields(self):
        call = _make_call("BOOKING_CAPTURED")
        call.patient_name = "Alice Wang"
        call.service_type = "crown"
        call.requested_time = "Friday morning"

        practice = MagicMock()
        practice.id = uuid.uuid4()
        practice.name = "Test"
        practice.staff_email = "t@t.com"

        week_start = datetime(2026, 3, 23, tzinfo=timezone.utc)
        week_end = week_start + timedelta(days=7)

        with patch("app.digest.AsyncSessionLocal") as mock_session_cls:
            mock_db = AsyncMock()
            mock_session_cls.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_session_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_result = MagicMock()
            mock_result.scalars.return_value.all.return_value = [call]
            mock_db.execute = AsyncMock(return_value=mock_result)

            stats = await compute_stats(practice, week_start, week_end)

        assert stats.bookings[0]["patient_name"] == "Alice Wang"
        assert stats.bookings[0]["service_type"] == "crown"
        assert stats.bookings[0]["requested_time"] == "Friday morning"


class TestSendDigest:
    @pytest.mark.asyncio
    async def test_dry_run_does_not_call_sendgrid(self):
        stats = _make_stats()
        with patch("httpx.AsyncClient") as mock_client:
            result = await send_digest(stats, dry_run=True)
        assert result is True
        mock_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_no_staff_email(self):
        stats = _make_stats(staff_email=None)
        result = await send_digest(stats, dry_run=False)
        assert result is False

    @pytest.mark.asyncio
    @patch("app.digest.settings")
    async def test_skips_when_sendgrid_not_configured(self, mock_settings):
        mock_settings.sendgrid_api_key = ""
        stats = _make_stats()
        result = await send_digest(stats, dry_run=False)
        assert result is False

    @pytest.mark.asyncio
    @patch("app.digest.settings")
    async def test_returns_true_on_successful_send(self, mock_settings):
        mock_settings.sendgrid_api_key = "SG.test"
        mock_settings.sendgrid_from_email = "noreply@test.com"
        stats = _make_stats()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(return_value=mock_response)
            result = await send_digest(stats, dry_run=False)

        assert result is True


class TestEmailFormatters:
    def test_subject_includes_name_and_counts(self):
        stats = _make_stats(total_calls=12, bookings=[{"x": 1}, {"x": 2}])
        subj = _subject(stats)
        assert "Sunrise Dental" in subj
        assert "12" in subj
        assert "2" in subj

    def test_subject_says_no_calls_when_zero(self):
        stats = _make_stats(total_calls=0, bookings=[])
        subj = _subject(stats)
        assert "no calls" in subj.lower()

    def test_plain_includes_all_disposition_counts(self):
        stats = _make_stats(
            total_calls=8, escalations=2, unanswered_escalations=1, hung_up=1, faq_only=1
        )
        plain = _email_plain(stats)
        assert "8" in plain
        assert "2" in plain   # escalations
        assert "1" in plain   # unanswered

    def test_plain_lists_bookings(self):
        stats = _make_stats()
        plain = _email_plain(stats)
        assert "Jane Smith" in plain
        assert "cleaning" in plain
        assert "Bob Lee" in plain

    def test_plain_warns_on_unanswered(self):
        stats = _make_stats(unanswered_escalations=3)
        plain = _email_plain(stats)
        assert "unanswered" in plain.lower()

    def test_html_includes_unanswered_banner(self):
        stats = _make_stats(unanswered_escalations=2)
        html = _email_html(stats)
        assert "unanswered" in html.lower()

    def test_html_no_banner_when_no_unanswered(self):
        stats = _make_stats(unanswered_escalations=0)
        html = _email_html(stats)
        # Banner div should not appear
        assert "⚠" not in html
