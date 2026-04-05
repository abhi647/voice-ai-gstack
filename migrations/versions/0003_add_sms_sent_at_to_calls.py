"""add sms_sent_at to calls

Tracks when a patient-facing booking confirmation SMS was sent.
NULL = not yet sent (or not applicable). Set by finalize_call on success.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "calls",
        sa.Column("sms_sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("calls", "sms_sent_at")
