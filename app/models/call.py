"""
Call — one row per inbound call.

Dispositions:
  BOOKING_CAPTURED  — agent captured intent; SMS+email sent to practice staff (v0.1)
  ESCALATED         — warm-transferred to human
  ESCALATED_UNANSWERED — escalation number didn't answer; callback SMS sent
  HUNG_UP           — patient hung up before resolution
  FAQ_ONLY          — answered a question, no booking needed
  DUPLICATE_PREVENTED — would have been a duplicate booking; confirmed instead
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    practice_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("practices.id"), nullable=False, index=True
    )
    twilio_call_sid: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    patient_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disposition: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Booking intent (v0.1 — no PMS write yet)
    patient_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requested_time: Mapped[str | None] = mapped_column(String(255), nullable=True)
    service_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Storage keys (populated after call ends)
    transcript_s3_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    audio_s3_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Full transcript text (also stored encrypted in S3; this is the searchable copy)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Patient-facing confirmation SMS — set when we successfully text the patient
    sms_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
