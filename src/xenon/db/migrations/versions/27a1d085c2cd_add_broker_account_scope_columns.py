"""add broker account scope columns

Revision ID: 27a1d085c2cd
Revises: eaec7f146df5
Create Date: 2026-04-26 20:21:33.131425

Adds three scope columns (broker, account_env, broker_account) to all
execution and portfolio tables, plus CHECK constraints, scoped indexes,
and a composite nav_history PK.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "27a1d085c2cd"
down_revision: Union[str, Sequence[str], None] = "eaec7f146df5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _add_scope_columns(table: str) -> None:
    op.add_column(
        table,
        sa.Column("broker", sa.Text(), server_default=sa.text("'IB'"), nullable=False),
        schema="xenon",
    )
    op.add_column(
        table,
        sa.Column("account_env", sa.Text(), server_default=sa.text("'legacy_unknown'"), nullable=False),
        schema="xenon",
    )
    op.add_column(
        table,
        sa.Column("broker_account", sa.Text(), server_default=sa.text("'legacy_unknown'"), nullable=False),
        schema="xenon",
    )


def upgrade() -> None:
    """Upgrade schema."""
    # ---- Execution tables: broker = 'IB' only ----
    _add_scope_columns("order_submissions")
    op.create_check_constraint(
        "ck_order_sub_broker_ib_only",
        "order_submissions",
        "broker = 'IB'",
        schema="xenon",
    )
    op.create_check_constraint(
        "ck_order_sub_account_env",
        "order_submissions",
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        schema="xenon",
    )
    op.drop_index(op.f("ix_order_sub_ib_order_id"), table_name="order_submissions", schema="xenon")
    op.create_index(
        "ix_order_sub_ib_order_id",
        "order_submissions",
        ["broker", "account_env", "broker_account", "ib_order_id"],
        unique=False,
        schema="xenon",
    )
    op.drop_index(op.f("ix_order_sub_perm_id"), table_name="order_submissions", schema="xenon")
    op.create_index(
        "ix_order_sub_perm_id",
        "order_submissions",
        ["broker", "account_env", "broker_account", "perm_id"],
        unique=False,
        schema="xenon",
    )
    op.drop_constraint(op.f("uq_order_sub_user_attempt"), "order_submissions", schema="xenon", type_="unique")
    op.create_unique_constraint(
        "uq_order_sub_user_attempt",
        "order_submissions",
        ["broker", "account_env", "broker_account", "user_id", "client_attempt_id"],
        schema="xenon",
    )

    _add_scope_columns("trades")
    op.create_check_constraint(
        "ck_trades_broker_ib_only",
        "trades",
        "broker = 'IB'",
        schema="xenon",
    )
    op.create_check_constraint(
        "ck_trades_account_env",
        "trades",
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        schema="xenon",
    )

    _add_scope_columns("wizard_sessions")
    op.create_check_constraint(
        "ck_wizard_sess_broker_ib_only",
        "wizard_sessions",
        "broker = 'IB'",
        schema="xenon",
    )
    op.create_check_constraint(
        "ck_wizard_sess_account_env",
        "wizard_sessions",
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        schema="xenon",
    )

    _add_scope_columns("wizard_combo_attempts")
    op.create_check_constraint(
        "ck_wizard_attempt_broker_ib_only",
        "wizard_combo_attempts",
        "broker = 'IB'",
        schema="xenon",
    )
    op.create_check_constraint(
        "ck_wizard_attempt_account_env",
        "wizard_combo_attempts",
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        schema="xenon",
    )

    # ---- Portfolio tables: broker IN ('IB', 'FUTU') ----
    _add_scope_columns("positions")
    op.create_check_constraint(
        "ck_positions_broker",
        "positions",
        "broker IN ('IB', 'FUTU')",
        schema="xenon",
    )
    op.create_check_constraint(
        "ck_positions_account_env",
        "positions",
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        schema="xenon",
    )

    _add_scope_columns("account_snapshots")
    op.create_check_constraint(
        "ck_acct_snap_broker",
        "account_snapshots",
        "broker IN ('IB', 'FUTU')",
        schema="xenon",
    )
    op.create_check_constraint(
        "ck_acct_snap_account_env",
        "account_snapshots",
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        schema="xenon",
    )

    # ---- nav_history: PK restructure ----
    # Existing rows (if any) all default to ('IB', 'legacy_unknown', 'legacy_unknown'),
    # so the composite PK remains unique because date alone was unique.
    op.execute("ALTER TABLE xenon.nav_history DROP CONSTRAINT nav_history_pkey")
    _add_scope_columns("nav_history")
    op.create_primary_key(
        "nav_history_pkey",
        "nav_history",
        ["broker", "account_env", "broker_account", "date"],
        schema="xenon",
    )
    op.create_check_constraint(
        "ck_nav_broker",
        "nav_history",
        "broker IN ('IB', 'FUTU')",
        schema="xenon",
    )
    op.create_check_constraint(
        "ck_nav_account_env",
        "nav_history",
        "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
        schema="xenon",
    )


def downgrade() -> None:
    """Downgrade schema."""
    # nav_history: restore date-only PK
    op.drop_constraint("ck_nav_account_env", "nav_history", schema="xenon", type_="check")
    op.drop_constraint("ck_nav_broker", "nav_history", schema="xenon", type_="check")
    op.execute("ALTER TABLE xenon.nav_history DROP CONSTRAINT nav_history_pkey")
    op.drop_column("nav_history", "broker_account", schema="xenon")
    op.drop_column("nav_history", "account_env", schema="xenon")
    op.drop_column("nav_history", "broker", schema="xenon")
    op.create_primary_key("nav_history_pkey", "nav_history", ["date"], schema="xenon")

    # account_snapshots
    op.drop_constraint("ck_acct_snap_account_env", "account_snapshots", schema="xenon", type_="check")
    op.drop_constraint("ck_acct_snap_broker", "account_snapshots", schema="xenon", type_="check")
    op.drop_column("account_snapshots", "broker_account", schema="xenon")
    op.drop_column("account_snapshots", "account_env", schema="xenon")
    op.drop_column("account_snapshots", "broker", schema="xenon")

    # positions
    op.drop_constraint("ck_positions_account_env", "positions", schema="xenon", type_="check")
    op.drop_constraint("ck_positions_broker", "positions", schema="xenon", type_="check")
    op.drop_column("positions", "broker_account", schema="xenon")
    op.drop_column("positions", "account_env", schema="xenon")
    op.drop_column("positions", "broker", schema="xenon")

    # wizard_combo_attempts
    op.drop_constraint("ck_wizard_attempt_account_env", "wizard_combo_attempts", schema="xenon", type_="check")
    op.drop_constraint("ck_wizard_attempt_broker_ib_only", "wizard_combo_attempts", schema="xenon", type_="check")
    op.drop_column("wizard_combo_attempts", "broker_account", schema="xenon")
    op.drop_column("wizard_combo_attempts", "account_env", schema="xenon")
    op.drop_column("wizard_combo_attempts", "broker", schema="xenon")

    # wizard_sessions
    op.drop_constraint("ck_wizard_sess_account_env", "wizard_sessions", schema="xenon", type_="check")
    op.drop_constraint("ck_wizard_sess_broker_ib_only", "wizard_sessions", schema="xenon", type_="check")
    op.drop_column("wizard_sessions", "broker_account", schema="xenon")
    op.drop_column("wizard_sessions", "account_env", schema="xenon")
    op.drop_column("wizard_sessions", "broker", schema="xenon")

    # trades
    op.drop_constraint("ck_trades_account_env", "trades", schema="xenon", type_="check")
    op.drop_constraint("ck_trades_broker_ib_only", "trades", schema="xenon", type_="check")
    op.drop_column("trades", "broker_account", schema="xenon")
    op.drop_column("trades", "account_env", schema="xenon")
    op.drop_column("trades", "broker", schema="xenon")

    # order_submissions
    op.drop_constraint("uq_order_sub_user_attempt", "order_submissions", schema="xenon", type_="unique")
    op.create_unique_constraint(
        op.f("uq_order_sub_user_attempt"),
        "order_submissions",
        ["user_id", "client_attempt_id"],
        schema="xenon",
    )
    op.drop_index("ix_order_sub_perm_id", table_name="order_submissions", schema="xenon")
    op.create_index(op.f("ix_order_sub_perm_id"), "order_submissions", ["perm_id"], unique=False, schema="xenon")
    op.drop_index("ix_order_sub_ib_order_id", table_name="order_submissions", schema="xenon")
    op.create_index(
        op.f("ix_order_sub_ib_order_id"), "order_submissions", ["ib_order_id"], unique=False, schema="xenon"
    )
    op.drop_constraint("ck_order_sub_account_env", "order_submissions", schema="xenon", type_="check")
    op.drop_constraint("ck_order_sub_broker_ib_only", "order_submissions", schema="xenon", type_="check")
    op.drop_column("order_submissions", "broker_account", schema="xenon")
    op.drop_column("order_submissions", "account_env", schema="xenon")
    op.drop_column("order_submissions", "broker", schema="xenon")
