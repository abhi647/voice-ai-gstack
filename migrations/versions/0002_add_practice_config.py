"""add practice config column

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "practices",
        sa.Column("config", postgresql.JSON(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "practices",
        sa.Column("staff_email", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("practices", "staff_email")
    op.drop_column("practices", "config")
