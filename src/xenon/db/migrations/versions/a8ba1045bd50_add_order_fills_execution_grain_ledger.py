"""add order_fills execution-grain ledger

Revision ID: a8ba1045bd50
Revises: 4f2a1c9e8b3d
Create Date: 2026-04-28 22:50:33.879855

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "a8ba1045bd50"
down_revision: Union[str, Sequence[str], None] = "4f2a1c9e8b3d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "order_fills",
        sa.Column("exec_id", sa.Text(), nullable=False),
        sa.Column("submission_id", sa.Text(), nullable=True),
        sa.Column("combo_attempt_id", sa.Text(), nullable=True),
        sa.Column("perm_id", sa.Text(), nullable=True),
        sa.Column("ib_order_id", sa.Text(), nullable=True),
        sa.Column("con_id", sa.BigInteger(), nullable=True),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(12, 4), nullable=False),
        sa.Column("commission", sa.Numeric(12, 4), server_default=sa.text("0"), nullable=True),
        sa.Column("filled_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("broker", sa.Text(), server_default=sa.text("'IB'"), nullable=False),
        sa.Column("account_env", sa.Text(), nullable=False),
        sa.Column("broker_account", sa.Text(), nullable=False),
        sa.CheckConstraint("broker IN ('IB','FUTU')", name="ck_fills_broker"),
        sa.CheckConstraint(
            "submission_id IS NOT NULL "
            "OR combo_attempt_id IS NOT NULL "
            "OR (metadata IS NOT NULL AND metadata ? 'legacy_source')",
            name="ck_fills_source_present",
        ),
        sa.ForeignKeyConstraint(
            ["combo_attempt_id"],
            ["xenon.wizard_combo_attempts.attempt_id"],
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["xenon.order_submissions.submission_id"],
        ),
        sa.PrimaryKeyConstraint("exec_id"),
        schema="xenon",
    )
    op.create_index(
        "ix_fills_combo_attempt",
        "order_fills",
        ["combo_attempt_id"],
        unique=False,
        schema="xenon",
    )
    op.create_index(
        "ix_fills_perm_id",
        "order_fills",
        ["broker", "account_env", "broker_account", "perm_id"],
        unique=False,
        schema="xenon",
    )
    op.create_index(
        "ix_fills_submission",
        "order_fills",
        ["submission_id"],
        unique=False,
        schema="xenon",
    )
    op.create_index(
        "ix_fills_ticker_time",
        "order_fills",
        ["ticker", "filled_at"],
        unique=False,
        schema="xenon",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_fills_ticker_time", table_name="order_fills", schema="xenon")
    op.drop_index("ix_fills_submission", table_name="order_fills", schema="xenon")
    op.drop_index("ix_fills_perm_id", table_name="order_fills", schema="xenon")
    op.drop_index("ix_fills_combo_attempt", table_name="order_fills", schema="xenon")
    op.drop_table("order_fills", schema="xenon")
