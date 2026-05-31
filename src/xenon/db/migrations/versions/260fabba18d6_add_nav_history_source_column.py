"""add nav_history source column

Revision ID: 260fabba18d6
Revises: ed8820930349
Create Date: 2026-06-01 01:18:29.193691

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '260fabba18d6'
down_revision: Union[str, Sequence[str], None] = 'ed8820930349'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add `source` column to xenon.nav_history.

    Existing rows backfilled to 'intraday' (honest about what they actually are
    — v1 has no EOD scheduler; that's a follow-up).
    Spec §12, Schema changes Migration 2.
    """
    op.add_column(
        "nav_history",
        sa.Column("source", sa.Text(), nullable=False, server_default="intraday"),
        schema="xenon",
    )
    op.create_check_constraint(
        "ck_nav_history_source",
        "nav_history",
        "source IN ('close', 'intraday')",
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_constraint("ck_nav_history_source", "nav_history", schema="xenon")
    op.drop_column("nav_history", "source", schema="xenon")
