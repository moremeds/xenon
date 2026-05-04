"""add events outbox dlq table

Revision ID: 34342e91f0f4
Revises: 20260504_pos_rules
Create Date: 2026-05-04 21:45:43.546395

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "34342e91f0f4"
down_revision: Union[str, Sequence[str], None] = "20260504_pos_rules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "outbox_dlq",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_event_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("dead_lettered_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        schema="events",
    )


def downgrade() -> None:
    op.drop_table("outbox_dlq", schema="events")
