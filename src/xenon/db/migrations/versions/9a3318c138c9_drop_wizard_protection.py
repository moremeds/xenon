"""drop wizard_protection

Revision ID: 9a3318c138c9
Revises: 34342e91f0f4
Create Date: 2026-05-04 22:04:22.865346

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "9a3318c138c9"
down_revision: Union[str, Sequence[str], None] = "34342e91f0f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(sa.text("SELECT COUNT(*) FROM xenon.wizard_protection")).scalar_one()
    if count != 0:
        raise RuntimeError(
            "wizard_protection has "
            f"{count} rows; manually rebase rows onto xenon.position_protection before dropping it."
        )
    op.drop_table("wizard_protection", schema="xenon")


def downgrade() -> None:
    op.create_table(
        "wizard_protection",
        sa.Column("protection_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("attempt_id", sa.Text()),
        sa.Column("protection_type", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("triggered_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["session_id"], ["xenon.wizard_sessions.session_id"]),
        sa.ForeignKeyConstraint(["attempt_id"], ["xenon.wizard_combo_attempts.attempt_id"]),
        sa.UniqueConstraint("session_id", name="uq_wizard_protection_session"),
        schema="xenon",
    )
