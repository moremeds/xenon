"""futu daily statement: page_text + financing + transaction_totals

Revision ID: 2026_06_03_futu_stmt2
Revises: 2026_06_03_futu_stmt
Create Date: 2026-06-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "2026_06_03_futu_stmt2"
down_revision: Union[str, Sequence[str], None] = "2026_06_03_futu_stmt"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "futu_daily_statement",
        sa.Column("page_text", JSONB, nullable=True),
        schema="xenon",
    )
    op.add_column(
        "futu_daily_statement",
        sa.Column("financing", JSONB, nullable=True),
        schema="xenon",
    )
    op.add_column(
        "futu_daily_statement",
        sa.Column("transaction_totals", JSONB, nullable=True),
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_column("futu_daily_statement", "transaction_totals", schema="xenon")
    op.drop_column("futu_daily_statement", "financing", schema="xenon")
    op.drop_column("futu_daily_statement", "page_text", schema="xenon")
