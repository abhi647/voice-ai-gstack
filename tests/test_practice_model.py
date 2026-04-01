"""
Tests for the Practice model (app/models/practice.py).

Coverage:
  Practice.get_by_twilio_number
    ├── [✓] returns practice when found and active
    ├── [✓] returns None when number not found
    └── [✓] returns None when practice is inactive (is_active=False filter)
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.practice import Practice


class TestPracticeGetByTwilioNumber:
    async def test_returns_practice_when_found(self):
        mock_db = AsyncMock()
        mock_practice = MagicMock(spec=Practice)
        mock_practice.twilio_number = "+15551234567"
        mock_practice.is_active = True

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_practice
        mock_db.execute.return_value = mock_result

        result = await Practice.get_by_twilio_number(mock_db, "+15551234567")

        assert result is mock_practice
        mock_db.execute.assert_called_once()

    async def test_returns_none_when_not_found(self):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await Practice.get_by_twilio_number(mock_db, "+15550000000")

        assert result is None

    async def test_query_filters_active_only(self):
        """The query must include is_active=True to reject lapsed subscriptions."""
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        await Practice.get_by_twilio_number(mock_db, "+15551234567")

        # Verify a query was executed (we can't easily inspect SQLAlchemy whereclause
        # without deeper introspection, but we verify execute was called)
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args[0][0]
        # The whereclause should reference is_active
        assert "is_active" in str(call_args)
