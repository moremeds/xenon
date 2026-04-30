"""add regime_state view and regime_overrides table

Revision ID: 48343156f9b7
Revises: 8a3f2c7d1e90
Create Date: 2026-04-30 16:51:44.750201

Per spec docs/superpowers/specs/2026-04-29-vcg-cri-strategies-rewiring-design.md
§4.2 (regime_state view) and §4.3 (regime_overrides table). The view is a
thin projection of the latest row of vcg_series and cri_series; tier
classification (NORMAL / EDR / TIER_2 / TIER_1 / PANIC / UNKNOWN) is computed
in Python so it is unit-testable in isolation. The audit table is keyed on
order_submissions.submission_id with a DEFERRABLE INITIALLY DEFERRED FK so
the override row can be inserted in the same transaction as the order
reservation.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "48343156f9b7"
down_revision: Union[str, Sequence[str], None] = "8a3f2c7d1e90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. regime_state view — thin projection of latest vcg_series + cri_series
    op.execute("""
        CREATE OR REPLACE VIEW xenon.regime_state AS
        WITH latest_vcg AS (
            SELECT
                scanned_at,
                tier            AS vcg_tier_raw,
                regime          AS vcg_regime,
                ro,
                edr,
                bounce,
                sign_ok,
                sign_suppressed,
                pi_panic,
                vix
            FROM xenon.vcg_series
            ORDER BY scanned_at DESC
            LIMIT 1
        ),
        latest_cri AS (
            SELECT
                recorded_at,
                cri_level       AS cri_score,
                crash_trigger_fired,
                cta_forced_reduction,
                vix             AS cri_vix
            FROM xenon.cri_series
            ORDER BY recorded_at DESC
            LIMIT 1
        )
        SELECT
            v.scanned_at        AS vcg_scanned_at,
            v.vcg_tier_raw,
            v.vcg_regime,
            v.ro                AS vcg_ro,
            v.edr               AS vcg_edr,
            v.bounce            AS vcg_bounce,
            v.sign_ok           AS vcg_sign_ok,
            v.sign_suppressed   AS vcg_sign_suppressed,
            v.pi_panic          AS vcg_pi_panic,
            v.vix               AS vcg_vix,
            c.recorded_at       AS cri_scanned_at,
            c.cri_score,
            c.crash_trigger_fired,
            c.cta_forced_reduction,
            c.cri_vix
        FROM latest_vcg v CROSS JOIN latest_cri c
    """)

    # 2. regime_overrides — per-scope audit, keyed on submission_id
    op.create_table(
        "regime_overrides",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "ts",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("account_env", sa.Text, nullable=False),
        sa.Column("broker", sa.Text, nullable=False),
        sa.Column("broker_account", sa.Text, nullable=False),
        sa.Column("submission_id", sa.Text, nullable=False),
        sa.Column("client_attempt_id", sa.Text),
        sa.Column("perm_id", sa.BigInteger),
        sa.Column("ib_order_id", sa.BigInteger),
        sa.Column("route", sa.Text, nullable=False),
        sa.Column("vcg_tier", sa.Text),
        sa.Column("cri_tier", sa.Text),
        sa.Column("binding_side", sa.Text, nullable=False),
        sa.Column("block_reason", sa.Text, nullable=False),
        sa.Column("user_reason", sa.Text, nullable=False),
        sa.Column("order_payload", postgresql.JSONB, nullable=False),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["xenon.order_submissions.submission_id"],
            name="fk_regime_overrides_submission",
            deferrable=True,
            initially="DEFERRED",
        ),
        schema="xenon",
    )

    op.create_index(
        "ix_regime_overrides_ts",
        "regime_overrides",
        [sa.text("ts DESC")],
        schema="xenon",
    )
    op.create_index(
        "ix_regime_overrides_submission",
        "regime_overrides",
        ["submission_id"],
        schema="xenon",
    )
    op.create_index(
        "ix_regime_overrides_user_ts",
        "regime_overrides",
        ["user_id", sa.text("ts DESC")],
        schema="xenon",
    )
    op.create_index(
        "ix_regime_overrides_scope_ts",
        "regime_overrides",
        ["account_env", "broker_account", sa.text("ts DESC")],
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_regime_overrides_scope_ts",
        table_name="regime_overrides",
        schema="xenon",
    )
    op.drop_index(
        "ix_regime_overrides_user_ts",
        table_name="regime_overrides",
        schema="xenon",
    )
    op.drop_index(
        "ix_regime_overrides_submission",
        table_name="regime_overrides",
        schema="xenon",
    )
    op.drop_index(
        "ix_regime_overrides_ts",
        table_name="regime_overrides",
        schema="xenon",
    )
    op.drop_table("regime_overrides", schema="xenon")
    op.execute("DROP VIEW IF EXISTS xenon.regime_state")
