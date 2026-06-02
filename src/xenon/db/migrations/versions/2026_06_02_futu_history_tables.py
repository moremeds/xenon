"""futu history tables — futu_trades + futu_cash_flow

Source-of-truth persistence for the backward NAV walk. Trades + cashflows
pulled from Futu OpenD live here once; downstream backfill reads from here
so we can re-derive nav_history without re-pulling from Futu (rate-limited).

v1 scope: US stocks only, USD-only cashflows. CHECK constraints enforce both.

Revision ID: 2026_06_02_futu
Revises: 489476c351cc
Create Date: 2026-06-02

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

# revision identifiers, used by Alembic.
revision: str = "2026_06_02_futu"
down_revision: Union[str, Sequence[str], None] = "489476c351cc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "futu_trades",
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_env", sa.Text(), nullable=False),
        sa.Column("broker_account", sa.Text(), nullable=False),
        sa.Column("futu_deal_id", sa.Text(), nullable=False),
        sa.Column("futu_order_id", sa.Text()),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("futu_code", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("price", sa.Numeric(14, 4), nullable=False),
        sa.Column("fees", sa.Numeric(14, 4), nullable=False, server_default=sa.text("0")),
        sa.Column("filled_at", TIMESTAMP(timezone=True), nullable=False),
        sa.Column("raw", JSONB, nullable=False),
        sa.Column(
            "ingested_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("broker", "account_env", "broker_account", "futu_deal_id"),
        sa.CheckConstraint("broker = 'FUTU'", name="ck_futu_trades_broker"),
        sa.CheckConstraint(
            "account_env IN ('paper', 'live', 'sim')",
            name="ck_futu_trades_account_env",
        ),
        sa.CheckConstraint("market = 'US'", name="ck_futu_trades_market_us_only"),
        sa.CheckConstraint("action IN ('BUY', 'SELL')", name="ck_futu_trades_action"),
        schema="xenon",
    )
    op.create_index(
        "ix_futu_trades_scope_filled_at",
        "futu_trades",
        ["broker", "account_env", "broker_account", "filled_at"],
        schema="xenon",
    )

    op.create_table(
        "futu_cash_flow",
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_env", sa.Text(), nullable=False),
        sa.Column("broker_account", sa.Text(), nullable=False),
        sa.Column("futu_flow_id", sa.Text(), nullable=False),
        sa.Column("cashflow_type", sa.Text(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 4), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("occurred_at", TIMESTAMP(timezone=True), nullable=False),
        sa.Column("raw", JSONB, nullable=False),
        sa.Column(
            "ingested_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("broker", "account_env", "broker_account", "futu_flow_id"),
        sa.CheckConstraint("broker = 'FUTU'", name="ck_futu_cash_flow_broker"),
        sa.CheckConstraint(
            "account_env IN ('paper', 'live', 'sim')",
            name="ck_futu_cash_flow_account_env",
        ),
        sa.CheckConstraint("currency = 'USD'", name="ck_futu_cash_flow_currency_usd_only"),
        sa.CheckConstraint(
            "cashflow_type IN ('DEPOSIT', 'WITHDRAW', 'TRANSFER_IN', 'TRANSFER_OUT')",
            name="ck_futu_cash_flow_type",
        ),
        schema="xenon",
    )
    op.create_index(
        "ix_futu_cash_flow_scope_occurred_at",
        "futu_cash_flow",
        ["broker", "account_env", "broker_account", "occurred_at"],
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_index("ix_futu_cash_flow_scope_occurred_at", "futu_cash_flow", schema="xenon")
    op.drop_table("futu_cash_flow", schema="xenon")
    op.drop_index("ix_futu_trades_scope_filled_at", "futu_trades", schema="xenon")
    op.drop_table("futu_trades", schema="xenon")
