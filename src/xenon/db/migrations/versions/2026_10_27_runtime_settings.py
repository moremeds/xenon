"""runtime_settings + runtime_settings_audit: DB overrides for runtime knobs

Revision ID: 2026_10_27_runtime_settings
Revises: 2026_10_24_futu_order_sub
Create Date: 2026-10-27

Additive. Two new tables, no existing table is touched:

- `xenon.runtime_settings` — one row per overridden key (registry:
  `xenon.runtime_settings`). No row = the env/default value applies, so an
  empty table keeps today's behavior.
- `xenon.runtime_settings_audit` — append-only change log. The API writes
  one row per PUT / DELETE in the same transaction as the change.

Downgrade drops both tables. It first refuses (raises, nothing changed)
when either table holds a row: dropping them would lose operator overrides
and their audit history.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

# revision identifiers, used by Alembic.
revision: str = "2026_10_27_runtime_settings"
down_revision: Union[str, Sequence[str], None] = "2026_10_24_futu_order_sub"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "runtime_settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", JSONB(), nullable=False),
        sa.Column("updated_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        schema="xenon",
    )
    op.create_table(
        "runtime_settings_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("old_value", JSONB(), nullable=True),
        sa.Column("new_value", JSONB(), nullable=True),
        sa.Column("changed_at", TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("changed_by", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        schema="xenon",
    )
    op.create_index(
        "ix_runtime_settings_audit_key_changed",
        "runtime_settings_audit",
        ["key", "changed_at"],
        schema="xenon",
    )


def downgrade() -> None:
    conn = op.get_bind()
    settings, audit = conn.execute(
        sa.text(
            "SELECT (SELECT count(*) FROM xenon.runtime_settings), (SELECT count(*) FROM xenon.runtime_settings_audit)"
        )
    ).one()
    if settings or audit:
        raise RuntimeError(
            f"runtime_settings holds {settings} row(s) and runtime_settings_audit {audit} row(s). "
            "No row was changed; the downgrade would drop them. Export or clear them first."
        )
    op.drop_index("ix_runtime_settings_audit_key_changed", table_name="runtime_settings_audit", schema="xenon")
    op.drop_table("runtime_settings_audit", schema="xenon")
    op.drop_table("runtime_settings", schema="xenon")
