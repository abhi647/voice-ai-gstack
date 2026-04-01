import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
async def async_client() -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def make_practice(**kwargs):
    """Build a mock Practice object for tests."""
    from unittest.mock import MagicMock
    from app.models.practice_config import PracticeConfig
    p = MagicMock()
    p.id = kwargs.get("id", uuid.uuid4())
    p.name = kwargs.get("name", "Sunrise Dental")
    p.twilio_number = kwargs.get("twilio_number", "+15551234567")
    p.escalation_number = kwargs.get("escalation_number", "+15559876543")
    p.timezone = kwargs.get("timezone", "America/New_York")
    p.state = kwargs.get("state", "NY")
    p.is_active = kwargs.get("is_active", True)
    p.stt_provider = kwargs.get("stt_provider", "deepgram")
    p.tts_provider = kwargs.get("tts_provider", "elevenlabs")
    p.staff_email = kwargs.get("staff_email", "front-desk@sunrise.com")
    p.get_config.return_value = kwargs.get("config", PracticeConfig())
    return p
