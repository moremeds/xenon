"""futu history tables — new Futu trades/cash_flow/corp_actions/recon_log/fx_daily
and rename nav_history.source → nav_type with 4-value union.

Spec: docs/superpowers/specs/2026-06-01-futu-account-performance-reconstruction-design.md
Plan: docs/superpowers/plans/2026-06-01-futu-account-performance-reconstruction.md

The plan originally added a new nav_history.source column with values
('reported','reconstructed'), but the existing column (added by 260fabba18d6,
constraint ck_nav_history_source) already discriminates ('close','intraday')
for IB intraday-vs-close snapshots. To avoid a column-name collision and a
two-state-machines-on-one-column footgun, we rename the existing column to
`nav_type` and broaden its check constraint to the four-value union:
   ('close', 'intraday', 'reported', 'reconstructed')

Existing IB writers continue to rely on the server_default='intraday'; new
Futu writers set 'reported' / 'reconstructed' explicitly.

Revision ID: 2026_06_01_0001
Revises: 489476c351cc
Create Date: 2026-06-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "2026_06_01_0001"
down_revision: Union[str, Sequence[str], None] = "489476c351cc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- 1. Rename nav_history.source → nav_type + broaden constraint ----
    op.drop_constraint("ck_nav_history_source", "nav_history", type_="check", schema="xenon")
    op.alter_column(
        "nav_history",
        "source",
        new_column_name="nav_type",
        schema="xenon",
    )
    op.create_check_constraint(
        "ck_nav_history_nav_type",
        "nav_history",
        "nav_type IN ('close', 'intraday', 'reported', 'reconstructed')",
        schema="xenon",
    )

    # ---- 2. futu_trades ----
    op.create_table(
        "futu_trades",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("futu_deal_id", sa.Text, nullable=False),
        sa.Column("futu_order_id", sa.Text, nullable=False),
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column("futu_code", sa.Text, nullable=False),
        sa.Column("market", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("price", sa.Numeric(12, 4), nullable=False),
        sa.Column("fees", sa.Numeric(10, 4), nullable=False, server_default="0"),
        sa.Column("realized_pnl", sa.Numeric(12, 2)),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw", JSONB, nullable=False),
        sa.Column("broker", sa.Text, nullable=False, server_default=sa.text("'FUTU'")),
        sa.Column("account_env", sa.Text, nullable=False),
        sa.Column("broker_account", sa.Text, nullable=False),
        sa.CheckConstraint("broker = 'FUTU'", name="ck_futu_trades_broker"),
        sa.CheckConstraint(
            "account_env IN ('paper','live','sim','legacy_unknown')",
            name="ck_futu_trades_env",
        ),
        sa.CheckConstraint("market = 'US'", name="ck_futu_trades_market_us"),
        sa.UniqueConstraint(
            "broker",
            "account_env",
            "broker_account",
            "futu_deal_id",
            name="uq_futu_trades_scope_deal",
        ),
        schema="xenon",
    )
    op.create_index(
        "ix_futu_trades_scope_filled",
        "futu_trades",
        ["broker", "account_env", "broker_account", "filled_at"],
        schema="xenon",
    )
    op.create_index(
        "ix_futu_trades_ticker_filled",
        "futu_trades",
        ["ticker", "filled_at"],
        schema="xenon",
    )

    # ---- 3. futu_cash_flow ----
    op.create_table(
        "futu_cash_flow",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("futu_flow_id", sa.Text, nullable=False),
        sa.Column("flow_type", sa.Text, nullable=False),
        sa.Column("futu_type_raw", sa.Text, nullable=False),
        sa.Column("amount", sa.Numeric(12, 2)),  # NULLABLE — missing-FX path
        sa.Column("amount_original", sa.Numeric(12, 2), nullable=False),
        sa.Column("original_currency", sa.Text, nullable=False),
        sa.Column("fx_rate_used", sa.Numeric(12, 6)),
        sa.Column("ticker", sa.Text),
        sa.Column("description", sa.Text),
        sa.Column("flow_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw", JSONB, nullable=False),
        sa.Column("broker", sa.Text, nullable=False, server_default=sa.text("'FUTU'")),
        sa.Column("account_env", sa.Text, nullable=False),
        sa.Column("broker_account", sa.Text, nullable=False),
        sa.CheckConstraint("broker = 'FUTU'", name="ck_futu_cash_flow_broker"),
        sa.CheckConstraint(
            "account_env IN ('paper','live','sim','legacy_unknown')",
            name="ck_futu_cash_flow_env",
        ),
        sa.CheckConstraint(
            "flow_type IN ('deposit','withdraw','dividend','interest','fee','other')",
            name="ck_futu_cash_flow_type",
        ),
        sa.UniqueConstraint(
            "broker",
            "account_env",
            "broker_account",
            "futu_flow_id",
            name="uq_futu_cash_flow_scope_flow",
        ),
        schema="xenon",
    )
    op.create_index(
        "ix_futu_cash_flow_scope_at",
        "futu_cash_flow",
        ["broker", "account_env", "broker_account", "flow_at"],
        schema="xenon",
    )

    # ---- 4. corporate_actions ----
    op.create_table(
        "corporate_actions",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("ticker", sa.Text, nullable=False),
        sa.Column("action_type", sa.Text, nullable=False),
        sa.Column("effective_date", sa.Date, nullable=False),
        sa.Column("ratio_numerator", sa.Numeric(8, 4)),
        sa.Column("ratio_denominator", sa.Numeric(8, 4)),
        sa.Column("new_ticker", sa.Text),
        sa.Column(
            "source",
            sa.Text,
            nullable=False,
            server_default=sa.text("'FUTU_GET_REHAB'"),
        ),
        sa.Column("notes", JSONB),
        sa.UniqueConstraint("ticker", "action_type", "effective_date", name="uq_corp_actions"),
        schema="xenon",
    )
    op.create_index(
        "ix_corp_actions_ticker_date",
        "corporate_actions",
        ["ticker", "effective_date"],
        schema="xenon",
    )

    # ---- 5. futu_reconciliation_log ----
    op.create_table(
        "futu_reconciliation_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("recon_date", sa.Date, nullable=False),
        sa.Column("reported_nav", sa.Numeric(14, 2)),
        sa.Column("reconstructed_nav", sa.Numeric(14, 2)),
        sa.Column("delta", sa.Numeric(14, 2)),
        sa.Column("threshold", sa.Numeric(14, 2)),
        sa.Column("exceeded", sa.Boolean),
        sa.Column("notes", JSONB),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("broker", sa.Text, nullable=False, server_default=sa.text("'FUTU'")),
        sa.Column("account_env", sa.Text, nullable=False),
        sa.Column("broker_account", sa.Text, nullable=False),
        sa.CheckConstraint("kind IN ('daily','anomaly')", name="ck_futu_recon_kind"),
        schema="xenon",
    )
    op.create_index(
        "uq_futu_recon_daily",
        "futu_reconciliation_log",
        ["broker", "account_env", "broker_account", "recon_date"],
        unique=True,
        postgresql_where=sa.text("kind = 'daily'"),
        schema="xenon",
    )
    op.create_index(
        "ix_futu_recon_kind_date",
        "futu_reconciliation_log",
        ["kind", "recon_date"],
        schema="xenon",
    )

    # ---- 6. fx_daily ----
    op.create_table(
        "fx_daily",
        sa.Column("currency", sa.Text, primary_key=True),
        sa.Column("date", sa.Date, primary_key=True),
        sa.Column("currency_per_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("source", sa.Text, nullable=False, server_default=sa.text("'FRED'")),
        schema="xenon",
    )

    # ---- 7. all_fills view (autogenerate ignores views) ----
    op.execute(
        """
        CREATE OR REPLACE VIEW xenon.all_fills AS
        SELECT 'IB'::text AS broker,
               f.account_env, f.broker_account,
               f.ticker, f.side AS action, f.qty AS quantity,
               f.price, f.commission AS fees, f.filled_at,
               NULL::numeric AS realized_pnl, NULL::jsonb AS raw
        FROM xenon.order_fills f WHERE f.broker = 'IB'
        UNION ALL
        SELECT 'FUTU'::text AS broker,
               t.account_env, t.broker_account,
               t.ticker, t.action, t.quantity,
               t.price, t.fees, t.filled_at,
               t.realized_pnl, t.raw
        FROM xenon.futu_trades t;
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS xenon.all_fills")
    op.drop_table("fx_daily", schema="xenon")
    op.drop_index(
        "ix_futu_recon_kind_date",
        table_name="futu_reconciliation_log",
        schema="xenon",
    )
    op.drop_index(
        "uq_futu_recon_daily",
        table_name="futu_reconciliation_log",
        schema="xenon",
    )
    op.drop_table("futu_reconciliation_log", schema="xenon")
    op.drop_index(
        "ix_corp_actions_ticker_date",
        table_name="corporate_actions",
        schema="xenon",
    )
    op.drop_table("corporate_actions", schema="xenon")
    op.drop_index(
        "ix_futu_cash_flow_scope_at",
        table_name="futu_cash_flow",
        schema="xenon",
    )
    op.drop_table("futu_cash_flow", schema="xenon")
    op.drop_index(
        "ix_futu_trades_ticker_filled",
        table_name="futu_trades",
        schema="xenon",
    )
    op.drop_index(
        "ix_futu_trades_scope_filled",
        table_name="futu_trades",
        schema="xenon",
    )
    op.drop_table("futu_trades", schema="xenon")
    # Restore nav_history.source name + original 2-value constraint.
    op.drop_constraint(
        "ck_nav_history_nav_type",
        "nav_history",
        type_="check",
        schema="xenon",
    )
    op.alter_column(
        "nav_history",
        "nav_type",
        new_column_name="source",
        schema="xenon",
    )
    op.create_check_constraint(
        "ck_nav_history_source",
        "nav_history",
        "source IN ('close', 'intraday')",
        schema="xenon",
    )
