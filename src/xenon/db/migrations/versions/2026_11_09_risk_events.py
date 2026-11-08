"""risk_events: tail-risk guard notification + ack log

Revision ID: 2026_11_09_risk_events
Revises: 2026_10_27_runtime_settings
Create Date: 2026-11-09

Additive. One new table, no existing table is touched. Schema approved by the
user 2026-11-08 (spec docs/superpowers/specs/2026-11-08-tail-risk-guard-design.md
§9). The API appends one row per risk-guard notification and per ack.

Downgrade drops the table. It first refuses (raises, nothing changed) when the
table holds a row: dropping it would lose the alert history.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

# revision identifiers, used by Alembic.
revision: str = "2026_11_09_risk_events"
down_revision: Union[str, Sequence[str], None] = "2026_10_27_runtime_settings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "risk_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_env", sa.Text(), nullable=False),
        sa.Column("broker_account", sa.Text(), nullable=False),
        sa.Column("occurred_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("incident_key", sa.Text(), nullable=False),
        sa.Column("incident_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("severity", sa.Integer(), nullable=False),
        sa.Column("detail", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        schema="xenon",
    )
    op.create_index(
        "ix_risk_events_scope_at",
        "risk_events",
        ["broker", "account_env", "broker_account", "occurred_at"],
        schema="xenon",
    )


def downgrade() -> None:
    rows = op.get_bind().execute(sa.text("SELECT count(*) FROM xenon.risk_events")).scalar_one()
    if rows:
        raise RuntimeError(
            f"risk_events holds {rows} row(s). No row was changed; the downgrade would drop them. "
            "Export or clear them first."
        )
    op.drop_index("ix_risk_events_scope_at", table_name="risk_events", schema="xenon")
    op.drop_table("risk_events", schema="xenon")
