"""add position_rules_review sidecar table

Revision ID: 821e494b1b71
Revises: 9a3318c138c9
Create Date: 2026-05-04 22:19:39.849341

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "821e494b1b71"
down_revision: Union[str, Sequence[str], None] = "9a3318c138c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "position_rules_review",
        sa.Column("review_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("protection_id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("reviewed_by", sa.Text(), nullable=False),
        sa.Column("reviewed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("note", sa.Text()),
        sa.CheckConstraint(
            "verdict IN ('expected','unexpected','structural')",
            name="ck_position_rules_review_verdict",
        ),
        sa.UniqueConstraint("event_id", name="uq_position_rules_review_event"),
        schema="xenon",
    )
    op.create_index(
        "ix_position_rules_review_protection",
        "position_rules_review",
        ["protection_id", "reviewed_at"],
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_index("ix_position_rules_review_protection", table_name="position_rules_review", schema="xenon")
    op.drop_table("position_rules_review", schema="xenon")
