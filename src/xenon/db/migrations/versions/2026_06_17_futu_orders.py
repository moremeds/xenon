"""futu_orders, futu_order_fees, futu_closed_trades + journal futu_close_id

Revision ID: 2026_06_17_futu_orders
Revises: adaaa7ea740d
Create Date: 2026-06-17

Read-only Futu order querying: persistence for open/historical orders, per-order
fees, and FIFO-reconstructed closed trades, plus a dedicated dedup column +
partial-unique index for FUTU_AUTO_IMPORT journal rows.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "2026_06_17_futu_orders"
down_revision: Union[str, Sequence[str], None] = "adaaa7ea740d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "futu_orders",
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_env", sa.Text(), nullable=False),
        sa.Column("broker_account", sa.Text(), nullable=False),
        sa.Column("futu_order_id", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("futu_code", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("order_type", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("limit_price", sa.Numeric(14, 4), nullable=True),
        sa.Column("aux_price", sa.Numeric(14, 4), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("tif", sa.Text(), server_default=sa.text("'DAY'"), nullable=False),
        sa.Column("filled_qty", sa.Numeric(20, 8), server_default=sa.text("0"), nullable=False),
        sa.Column("avg_fill_price", sa.Numeric(14, 4), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.Column("ingested_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("broker", "account_env", "broker_account", "futu_order_id"),
        sa.CheckConstraint("broker = 'FUTU'", name="ck_futu_orders_broker"),
        sa.CheckConstraint("account_env IN ('paper', 'live', 'sim')", name="ck_futu_orders_account_env"),
        sa.CheckConstraint("action IN ('BUY', 'SELL')", name="ck_futu_orders_action"),
        schema="xenon",
    )
    op.create_index(
        "ix_futu_orders_scope_updated_at",
        "futu_orders",
        ["broker", "account_env", "broker_account", "updated_at"],
        unique=False,
        schema="xenon",
    )
    op.create_index(
        "ix_futu_orders_scope_status",
        "futu_orders",
        ["broker", "account_env", "broker_account", "status"],
        unique=False,
        schema="xenon",
    )

    op.create_table(
        "futu_order_fees",
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_env", sa.Text(), nullable=False),
        sa.Column("broker_account", sa.Text(), nullable=False),
        sa.Column("futu_order_id", sa.Text(), nullable=False),
        sa.Column("total_fee", sa.Numeric(14, 4), server_default=sa.text("0"), nullable=False),
        sa.Column("currency", sa.Text(), server_default=sa.text("'USD'"), nullable=False),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.Column("ingested_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("broker", "account_env", "broker_account", "futu_order_id"),
        sa.CheckConstraint("broker = 'FUTU'", name="ck_futu_order_fees_broker"),
        schema="xenon",
    )

    op.create_table(
        "futu_closed_trades",
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_env", sa.Text(), nullable=False),
        sa.Column("broker_account", sa.Text(), nullable=False),
        sa.Column("futu_close_id", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("futu_code", sa.Text(), nullable=False),
        sa.Column("structure", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("entry_cost", sa.Numeric(14, 4), nullable=True),
        sa.Column("exit_cost", sa.Numeric(14, 4), nullable=True),
        sa.Column("realized_pnl", sa.Numeric(14, 2), nullable=False),
        sa.Column("cost_basis", sa.Numeric(14, 4), nullable=False),
        sa.Column("proceeds", sa.Numeric(14, 4), nullable=False),
        sa.Column("opened_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("closed_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column("ingested_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("broker", "account_env", "broker_account", "futu_close_id"),
        sa.CheckConstraint("broker = 'FUTU'", name="ck_futu_closed_trades_broker"),
        sa.CheckConstraint("account_env IN ('paper', 'live', 'sim')", name="ck_futu_closed_trades_account_env"),
        schema="xenon",
    )
    op.create_index(
        "ix_futu_closed_scope_closed_at",
        "futu_closed_trades",
        ["broker", "account_env", "broker_account", "closed_at"],
        unique=False,
        schema="xenon",
    )

    op.add_column("journal_entries", sa.Column("futu_close_id", sa.Text(), nullable=True), schema="xenon")
    op.create_index(
        "uq_journal_futu_auto_import",
        "journal_entries",
        ["broker", "account_env", "broker_account", "futu_close_id"],
        unique=True,
        schema="xenon",
        postgresql_where=sa.text("decision = 'FUTU_AUTO_IMPORT' AND futu_close_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_journal_futu_auto_import", table_name="journal_entries", schema="xenon")
    op.drop_column("journal_entries", "futu_close_id", schema="xenon")
    op.drop_index("ix_futu_closed_scope_closed_at", table_name="futu_closed_trades", schema="xenon")
    op.drop_table("futu_closed_trades", schema="xenon")
    op.drop_table("futu_order_fees", schema="xenon")
    op.drop_index("ix_futu_orders_scope_status", table_name="futu_orders", schema="xenon")
    op.drop_index("ix_futu_orders_scope_updated_at", table_name="futu_orders", schema="xenon")
    op.drop_table("futu_orders", schema="xenon")
