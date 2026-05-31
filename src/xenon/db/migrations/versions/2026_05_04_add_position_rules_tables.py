"""add position rules tables

Revision ID: 20260504_pos_rules
Revises: 9f2c4a1d8e57
Create Date: 2026-05-04
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260504_pos_rules"
down_revision: Union[str, Sequence[str], None] = "9f2c4a1d8e57"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "position_protection",
        sa.Column("protection_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_env", sa.Text(), nullable=False),
        sa.Column("broker_account", sa.Text(), nullable=False),
        sa.Column("position_key", sa.Text(), nullable=False),
        sa.Column("position_descriptor", postgresql.JSONB(), nullable=False),
        sa.Column("asset_class", sa.Text(), nullable=False),
        sa.Column("rule_kind", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'PENDING_ARM'")),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("state_data", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("native_order_perm_id", sa.BigInteger(), nullable=True),
        sa.Column("native_order_state", sa.Text(), nullable=True),
        sa.Column("armed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("triggered_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("last_evaluated_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("broker IN ('IB','FUTU')", name="ck_position_protection_broker"),
        sa.CheckConstraint(
            "account_env IN ('paper','live','sim','legacy_unknown')",
            name="ck_position_protection_account_env",
        ),
        sa.CheckConstraint(
            "state IN ('PENDING_ARM','ARMED','TRIGGERED','CLOSED','CANCELED','FAILED','SUPERSEDED')",
            name="ck_position_protection_state",
        ),
        sa.CheckConstraint(
            "rule_kind IN ('stop_loss','trailing_tp','take_profit_fixed','combo_tp_alert')",
            name="ck_position_protection_rule_kind",
        ),
        sa.CheckConstraint(
            "asset_class IN ('stock','long_option','debit_combo','credit_spread','covered_call','unclassified')",
            name="ck_position_protection_asset_class",
        ),
        schema="xenon",
    )
    op.create_index(
        "uq_position_protection_active",
        "position_protection",
        ["broker", "account_env", "broker_account", "position_key", "rule_kind"],
        unique=True,
        postgresql_where=sa.text("state IN ('PENDING_ARM','ARMED','TRIGGERED')"),
        schema="xenon",
    )
    op.create_index(
        "ix_position_protection_hot",
        "position_protection",
        ["state", "broker", "account_env", "broker_account"],
        postgresql_where=sa.text("state IN ('PENDING_ARM','ARMED')"),
        schema="xenon",
    )
    op.create_index(
        "ix_position_protection_lookup",
        "position_protection",
        ["broker", "account_env", "broker_account", "position_key"],
        schema="xenon",
    )

    op.create_table(
        "bracket_policies",
        sa.Column("policy_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("broker", sa.Text(), nullable=True),
        sa.Column("account_env", sa.Text(), nullable=True),
        sa.Column("broker_account", sa.Text(), nullable=True),
        sa.Column("asset_class", sa.Text(), nullable=False),
        sa.Column("rule_kind", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column("auto_place", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "rule_kind IN ('stop_loss','trailing_tp','take_profit_fixed','combo_tp_alert')",
            name="ck_bracket_policies_rule_kind",
        ),
        sa.CheckConstraint(
            "asset_class IN ('stock','long_option','debit_combo','credit_spread','covered_call','unclassified')",
            name="ck_bracket_policies_asset_class",
        ),
        schema="xenon",
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_bracket_policies_scope_class_kind
        ON xenon.bracket_policies (
            COALESCE(broker,'*'),
            COALESCE(account_env,'*'),
            COALESCE(broker_account,'*'),
            asset_class,
            rule_kind
        )
        """
    )

    op.create_table(
        "position_close_claims",
        sa.Column("claim_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_env", sa.Text(), nullable=False),
        sa.Column("broker_account", sa.Text(), nullable=False),
        sa.Column("position_key", sa.Text(), nullable=False),
        sa.Column("claimed_by_protection_id", sa.BigInteger(), nullable=False),
        sa.Column("claim_kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'PENDING'")),
        sa.Column("order_ref", sa.Text(), nullable=False),
        sa.Column("broker_perm_id", sa.BigInteger(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("claimed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("submitted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint("broker IN ('IB','FUTU')", name="ck_position_close_claims_broker"),
        sa.CheckConstraint(
            "account_env IN ('paper','live','sim','legacy_unknown')",
            name="ck_position_close_claims_account_env",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','SUBMITTED','FILLED','FAILED','ABANDONED')",
            name="ck_position_close_claims_status",
        ),
        sa.CheckConstraint(
            "claim_kind IN ('synthetic_close','native_reconcile_close')",
            name="ck_position_close_claims_kind",
        ),
        sa.UniqueConstraint("order_ref", name="uq_position_close_claims_order_ref"),
        schema="xenon",
    )
    op.create_index(
        "uq_position_close_claims_inflight",
        "position_close_claims",
        ["broker", "account_env", "broker_account", "position_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING','SUBMITTED')"),
        schema="xenon",
    )
    op.create_index(
        "ix_position_close_claims_cleanup",
        "position_close_claims",
        ["broker", "account_env", "broker_account", "status"],
        schema="xenon",
    )

    op.execute(
        """
        INSERT INTO xenon.bracket_policies (asset_class, rule_kind, auto_place, config) VALUES
          ('stock',          'stop_loss',          TRUE, '{"threshold_pct": -0.08, "anchor": "entry_price"}'),
          ('stock',          'trailing_tp',        TRUE, '{"trail_pct": 0.05, "activation_pct": 0.0, "anchor": "mfe"}'),
          ('long_option',    'stop_loss',          TRUE, '{"threshold_pct": -0.20, "anchor": "entry_price"}'),
          ('long_option',    'trailing_tp',        TRUE, '{"trail_pct": 0.25, "activation_pct": 0.30, "anchor": "mfe"}'),
          ('debit_combo',    'stop_loss',          TRUE, '{"threshold_pct_of_max_loss": 0.50, "anchor": "synthetic_mark"}'),
          ('debit_combo',    'trailing_tp',        TRUE, '{"trail_pct": 0.25, "activation_pct_of_max_gain": 0.25, "anchor": "mfe_pnl_dollars"}'),
          ('credit_spread',  'stop_loss',          TRUE, '{"trigger_kind": "either", "mark_multiple_of_credit": 2.0, "underlying_breach_short_strike": true, "anchor": "synthetic_mark"}'),
          ('credit_spread',  'take_profit_fixed',  TRUE, '{"close_at_credit_pct": 0.50, "anchor": "synthetic_mark"}')
        """
    )


def downgrade() -> None:
    op.drop_index("ix_position_close_claims_cleanup", table_name="position_close_claims", schema="xenon")
    op.drop_index("uq_position_close_claims_inflight", table_name="position_close_claims", schema="xenon")
    op.drop_table("position_close_claims", schema="xenon")
    op.execute("DROP INDEX IF EXISTS xenon.uq_bracket_policies_scope_class_kind")
    op.drop_table("bracket_policies", schema="xenon")
    op.drop_index("ix_position_protection_lookup", table_name="position_protection", schema="xenon")
    op.drop_index("ix_position_protection_hot", table_name="position_protection", schema="xenon")
    op.drop_index("uq_position_protection_active", table_name="position_protection", schema="xenon")
    op.drop_table("position_protection", schema="xenon")
