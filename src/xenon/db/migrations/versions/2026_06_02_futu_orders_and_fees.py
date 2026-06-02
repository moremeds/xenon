"""futu orders and order fees

Revision ID: 2026_06_02_futu_orders
Revises: 2026_06_02_cf_open
Create Date: 2026-06-02

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

# revision identifiers, used by Alembic.
revision: str = "2026_06_02_futu_orders"
down_revision: Union[str, Sequence[str], None] = "2026_06_02_cf_open"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "futu_orders",
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_env", sa.Text(), nullable=False),
        sa.Column("broker_account", sa.Text(), nullable=False),
        sa.Column("futu_order_id", sa.Text(), nullable=False),
        sa.Column("ticker", sa.Text()),
        sa.Column("futu_code", sa.Text()),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("trd_side", sa.Text()),
        sa.Column("order_type", sa.Text(), nullable=False),
        sa.Column("order_status", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8)),
        sa.Column("price", sa.Numeric(14, 4)),
        sa.Column("dealt_quantity", sa.Numeric(20, 8)),
        sa.Column("dealt_avg_price", sa.Numeric(14, 4)),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("created_at", TIMESTAMP(timezone=True), nullable=False),
        sa.Column("updated_at", TIMESTAMP(timezone=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'history_order'")),
        sa.Column("is_combo", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("leg_count", sa.Integer()),
        sa.Column("raw", JSONB, nullable=False),
        sa.Column(
            "ingested_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("broker", "account_env", "broker_account", "futu_order_id"),
        sa.CheckConstraint("broker = 'FUTU'", name="ck_futu_orders_broker"),
        sa.CheckConstraint(
            "account_env IN ('paper', 'live', 'sim')",
            name="ck_futu_orders_account_env",
        ),
        sa.CheckConstraint("market = 'US'", name="ck_futu_orders_market_us_only"),
        sa.CheckConstraint("currency = 'USD'", name="ck_futu_orders_currency_usd_only"),
        sa.CheckConstraint("source IN ('history_order', 'deal_derived')", name="ck_futu_orders_source"),
        schema="xenon",
    )
    op.create_index(
        "ix_futu_orders_scope_created_at",
        "futu_orders",
        ["broker", "account_env", "broker_account", "created_at"],
        schema="xenon",
    )

    op.create_table(
        "futu_order_fees",
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_env", sa.Text(), nullable=False),
        sa.Column("broker_account", sa.Text(), nullable=False),
        sa.Column("futu_order_id", sa.Text(), nullable=False),
        sa.Column("fee_amount", sa.Numeric(14, 4), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("raw", JSONB, nullable=False),
        sa.Column(
            "ingested_at",
            TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("broker", "account_env", "broker_account", "futu_order_id"),
        sa.ForeignKeyConstraint(
            ["broker", "account_env", "broker_account", "futu_order_id"],
            [
                "xenon.futu_orders.broker",
                "xenon.futu_orders.account_env",
                "xenon.futu_orders.broker_account",
                "xenon.futu_orders.futu_order_id",
            ],
            name="fk_futu_order_fees_order",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("broker = 'FUTU'", name="ck_futu_order_fees_broker"),
        sa.CheckConstraint(
            "account_env IN ('paper', 'live', 'sim')",
            name="ck_futu_order_fees_account_env",
        ),
        sa.CheckConstraint("currency = 'USD'", name="ck_futu_order_fees_currency_usd_only"),
        schema="xenon",
    )
    op.create_foreign_key(
        "fk_futu_trades_order",
        "futu_trades",
        "futu_orders",
        ["broker", "account_env", "broker_account", "futu_order_id"],
        ["broker", "account_env", "broker_account", "futu_order_id"],
        source_schema="xenon",
        referent_schema="xenon",
    )


def downgrade() -> None:
    op.drop_constraint("fk_futu_trades_order", "futu_trades", schema="xenon", type_="foreignkey")
    op.drop_table("futu_order_fees", schema="xenon")
    op.drop_index("ix_futu_orders_scope_created_at", "futu_orders", schema="xenon")
    op.drop_table("futu_orders", schema="xenon")
