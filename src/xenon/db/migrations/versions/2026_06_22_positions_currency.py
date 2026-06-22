"""add currency + exchange columns to positions

Revision ID: 2026_06_22_positions_currency
Revises: 2026_06_17_futu_orders
Create Date: 2026-06-22

Japan/Korea cash-equity support: positions now carry the contract's native
currency (JPY/KRW/USD) and its listing exchange (TSEJ/KRX/SMART/...). Existing
rows backfill to 'USD' (the prior implicit assumption); exchange is nullable.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2026_06_22_positions_currency"
down_revision: Union[str, Sequence[str], None] = "2026_06_17_futu_orders"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column("currency", sa.Text(), nullable=False, server_default=sa.text("'USD'")),
        schema="xenon",
    )
    op.add_column(
        "positions",
        sa.Column("exchange", sa.Text(), nullable=True),
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_column("positions", "exchange", schema="xenon")
    op.drop_column("positions", "currency", schema="xenon")
