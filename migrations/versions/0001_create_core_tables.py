"""create core tables

Revision ID: 0001
Revises:
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "practices",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("twilio_number", sa.String(20), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("escalation_number", sa.String(20), nullable=False),
        sa.Column("timezone", sa.String(50), nullable=False, server_default="America/New_York"),
        sa.Column("state", sa.String(2), nullable=False, server_default="NY"),
        sa.Column("stripe_customer_id", sa.String(255), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(255), nullable=True),
        sa.Column("stt_provider", sa.String(50), nullable=False, server_default="deepgram"),
        sa.Column("tts_provider", sa.String(50), nullable=False, server_default="elevenlabs"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_practices_twilio_number", "practices", ["twilio_number"])

    op.create_table(
        "calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "practice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("practices.id"),
            nullable=False,
        ),
        sa.Column("twilio_call_sid", sa.String(50), nullable=False, unique=True),
        sa.Column("patient_phone", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disposition", sa.String(50), nullable=True),
        sa.Column("patient_name", sa.String(255), nullable=True),
        sa.Column("requested_time", sa.String(255), nullable=True),
        sa.Column("service_type", sa.String(255), nullable=True),
        sa.Column("transcript_s3_key", sa.String(500), nullable=True),
        sa.Column("audio_s3_key", sa.String(500), nullable=True),
        sa.Column("transcript", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_calls_practice_id", "calls", ["practice_id"])

    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "practice_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("practices.id"),
            nullable=False,
        ),
        sa.Column(
            "call_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("calls.id"),
            nullable=True,
        ),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_log_practice_id", "audit_log", ["practice_id"])
    op.create_index("ix_audit_log_call_id", "audit_log", ["call_id"])
    op.create_index("ix_audit_log_ts", "audit_log", ["ts"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("calls")
    op.drop_table("practices")
