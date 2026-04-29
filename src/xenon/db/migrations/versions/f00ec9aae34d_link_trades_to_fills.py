"""link trades to fills

Revision ID: f00ec9aae34d
Revises: a8ba1045bd50
Create Date: 2026-04-28 23:07:47.552539

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f00ec9aae34d"
down_revision: Union[str, Sequence[str], None] = "a8ba1045bd50"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("trades", sa.Column("submission_id", sa.Text(), nullable=True), schema="xenon")
    op.add_column("trades", sa.Column("combo_attempt_id", sa.Text(), nullable=True), schema="xenon")
    op.add_column(
        "trades",
        sa.Column("state", sa.Text(), server_default=sa.text("'OPEN'"), nullable=False),
        schema="xenon",
    )
    op.create_foreign_key(
        "fk_trades_submission_id_order_submissions",
        "trades",
        "order_submissions",
        ["submission_id"],
        ["submission_id"],
        source_schema="xenon",
        referent_schema="xenon",
    )
    op.create_foreign_key(
        "fk_trades_combo_attempt_id_wizard_combo_attempts",
        "trades",
        "wizard_combo_attempts",
        ["combo_attempt_id"],
        ["attempt_id"],
        source_schema="xenon",
        referent_schema="xenon",
    )
    op.create_check_constraint(
        "ck_trades_state",
        "trades",
        "state IN ('OPEN','PARTIALLY_FILLED','CLOSED')",
        schema="xenon",
    )
    op.execute("UPDATE xenon.trades SET state = 'CLOSED' WHERE closed_at IS NOT NULL")
    op.execute("UPDATE xenon.trades SET state = 'OPEN' WHERE closed_at IS NULL")
    op.create_index("ix_trades_submission", "trades", ["submission_id"], unique=False, schema="xenon")
    op.create_index("ix_trades_combo_attempt", "trades", ["combo_attempt_id"], unique=False, schema="xenon")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_trades_combo_attempt", table_name="trades", schema="xenon")
    op.drop_index("ix_trades_submission", table_name="trades", schema="xenon")
    op.drop_constraint("ck_trades_state", "trades", schema="xenon", type_="check")
    op.drop_constraint(
        "fk_trades_combo_attempt_id_wizard_combo_attempts",
        "trades",
        schema="xenon",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_trades_submission_id_order_submissions",
        "trades",
        schema="xenon",
        type_="foreignkey",
    )
    op.drop_column("trades", "state", schema="xenon")
    op.drop_column("trades", "combo_attempt_id", schema="xenon")
    op.drop_column("trades", "submission_id", schema="xenon")
