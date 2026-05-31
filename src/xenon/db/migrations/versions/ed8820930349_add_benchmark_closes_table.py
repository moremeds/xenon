"""add benchmark_closes table

Revision ID: ed8820930349
Revises: 9f2c4a1d8e57
Create Date: 2026-06-01 01:18:29.033928

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ed8820930349'
down_revision: Union[str, Sequence[str], None] = '9f2c4a1d8e57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create xenon.benchmark_closes cache table.

    Spec: docs/superpowers/specs/2026-05-31-performance-rebuild-design.md
          § Schema changes, Migration 1.
    """
    op.create_table(
        "benchmark_closes",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("close", sa.Numeric(14, 4), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "date"),
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_table("benchmark_closes", schema="xenon")
