"""extend nav_history with cash/stock_value/options_value/total

Revision ID: 7c1e3a9b2f01
Revises: f00ec9aae34d
Create Date: 2026-05-03 14:00:00.000000

Replaces data/nav_history.jsonl + data/nav_history_ib.json with PG-only storage.
The IB Flex NAV importer (`fetch_ib_nav_series`) returns
`{date, total, cash, stock, options}`; persist all four so portfolio_performance
can drop both JSON readers.

`nav` (existing) — computed NAV used by ib_sync's daily upsert (compatible with
old JSONL writer).
`total` — IB Flex EquitySummaryByReportDateInBase total. Authoritative when
present; semantically equal to `nav` for IB but the column is kept distinct so
mixed sources (IB Flex + Futu fallback) don't conflict.
`cash` / `stock_value` / `options_value` — the three components of `total` from
IB Flex. NULL when no Flex data is available.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c1e3a9b2f01"
down_revision: Union[str, Sequence[str], None] = "f00ec9aae34d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("nav_history", sa.Column("total", sa.Numeric(14, 2), nullable=True), schema="xenon")
    op.add_column("nav_history", sa.Column("cash", sa.Numeric(14, 2), nullable=True), schema="xenon")
    op.add_column("nav_history", sa.Column("stock_value", sa.Numeric(14, 2), nullable=True), schema="xenon")
    op.add_column("nav_history", sa.Column("options_value", sa.Numeric(14, 2), nullable=True), schema="xenon")


def downgrade() -> None:
    op.drop_column("nav_history", "options_value", schema="xenon")
    op.drop_column("nav_history", "stock_value", schema="xenon")
    op.drop_column("nav_history", "cash", schema="xenon")
    op.drop_column("nav_history", "total", schema="xenon")
