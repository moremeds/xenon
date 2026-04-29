"""add journal_entries table

Revision ID: 2e8d6c8a19b4
Revises: f00ec9aae34d
Create Date: 2026-04-28 00:20:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "2e8d6c8a19b4"
down_revision: Union[str, Sequence[str], None] = "f00ec9aae34d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("trade_id", sa.BigInteger(), nullable=True),
        sa.Column("ticker", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("attachments", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("authored_by", sa.Text(), nullable=True),
        sa.Column("authored_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("broker", sa.Text(), server_default=sa.text("'IB'"), nullable=False),
        sa.Column("account_env", sa.Text(), server_default=sa.text("'legacy_unknown'"), nullable=False),
        sa.Column("broker_account", sa.Text(), server_default=sa.text("'legacy_unknown'"), nullable=False),
        sa.CheckConstraint("broker IN ('IB','FUTU')", name="ck_journal_broker"),
        sa.CheckConstraint(
            "account_env IN ('paper', 'live', 'sim', 'legacy_unknown')",
            name="ck_journal_account_env",
        ),
        sa.ForeignKeyConstraint(["trade_id"], ["xenon.trades.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema="xenon",
    )
    op.create_index(
        "ix_journal_scope_at",
        "journal_entries",
        ["broker", "account_env", "broker_account", "authored_at"],
        unique=False,
        schema="xenon",
    )
    op.create_index(
        "ix_journal_ticker_at",
        "journal_entries",
        ["ticker", "authored_at"],
        unique=False,
        schema="xenon",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_journal_ticker_at", table_name="journal_entries", schema="xenon")
    op.drop_index("ix_journal_scope_at", table_name="journal_entries", schema="xenon")
    op.drop_table("journal_entries", schema="xenon")
