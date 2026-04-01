"""
Practice — one row per dental/vet practice.

Data model:
  practice
    ├── id (UUID, PK)
    ├── twilio_number (E.164, unique) — inbound call lookup key
    ├── name
    ├── escalation_number (E.164) — where to warm-transfer emergencies
    ├── timezone (IANA, e.g. "America/New_York") — for after-hours calculation
    ├── state (2-letter US state) — for CA two-party consent disclosure
    ├── stripe_customer_id — billing anchor
    ├── stripe_subscription_id
    ├── stt_provider ("deepgram" | "sarvam")
    ├── tts_provider ("elevenlabs" | "cartesia" | "polly")
    ├── is_active — false = subscription lapsed, reject calls
    └── created_at / updated_at
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import select

from app.database import AsyncSession, Base


class Practice(Base):
    __tablename__ = "practices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    twilio_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    escalation_number: Mapped[str] = mapped_column(String(20), nullable=False)
    timezone: Mapped[str] = mapped_column(String(50), default="America/New_York")
    state: Mapped[str] = mapped_column(String(2), default="NY")
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stt_provider: Mapped[str] = mapped_column(String(50), default="deepgram")
    tts_provider: Mapped[str] = mapped_column(String(50), default="elevenlabs")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    @classmethod
    async def get_by_twilio_number(
        cls, db: AsyncSession, twilio_number: str
    ) -> "Practice | None":
        result = await db.execute(
            select(cls).where(cls.twilio_number == twilio_number, cls.is_active == True)
        )
        return result.scalar_one_or_none()
