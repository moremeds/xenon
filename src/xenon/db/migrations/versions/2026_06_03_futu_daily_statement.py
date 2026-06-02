"""futu daily statement table

Revision ID: 2026_06_03_futu_stmt
Revises: 2026_06_02_cf_open
Create Date: 2026-06-03

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP

revision: str = "2026_06_03_futu_stmt"
down_revision: Union[str, Sequence[str], None] = "2026_06_02_cf_open"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "futu_daily_statement",
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_env", sa.Text(), nullable=False),
        sa.Column("broker_account", sa.Text(), nullable=False),
        sa.Column("statement_date", sa.Date(), nullable=False),
        sa.Column("preparation_date", sa.Date(), nullable=False),
        sa.Column("account_number", sa.Text(), nullable=False),
        sa.Column("account_suffix", sa.Text(), nullable=False),
        sa.Column("client_name", sa.Text(), nullable=False),
        sa.Column("base_currency", sa.Text(), nullable=False),
        sa.Column("starting_portfolio_base", sa.Numeric(20, 4), nullable=False),
        sa.Column("ending_portfolio_base", sa.Numeric(20, 4), nullable=False),
        sa.Column("starting_funds_base", sa.Numeric(20, 4), nullable=False),
        sa.Column("ending_funds_base", sa.Numeric(20, 4), nullable=False),
        sa.Column("starting_cash_base", sa.Numeric(20, 4), nullable=False),
        sa.Column("ending_cash_base", sa.Numeric(20, 4), nullable=False),
        sa.Column("starting_nav_base", sa.Numeric(20, 4), nullable=False),
        sa.Column("ending_nav_base", sa.Numeric(20, 4), nullable=False),
        sa.Column("starting_nav_by_currency", JSONB, nullable=False),
        sa.Column("ending_nav_by_currency", JSONB, nullable=False),
        sa.Column("exchange_rates", JSONB, nullable=False),
        sa.Column("raw_pdf", sa.LargeBinary(), nullable=True),
        sa.Column("source_uid", sa.Text(), nullable=True),
        sa.Column("source_subject", sa.Text(), nullable=True),
        sa.Column(
            "ingested_at",
            TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("broker", "account_env", "broker_account", "statement_date"),
        sa.CheckConstraint("broker = 'FUTU'", name="ck_futu_daily_statement_broker"),
        sa.CheckConstraint(
            "account_env IN ('paper', 'live', 'sim')",
            name="ck_futu_daily_statement_account_env",
        ),
        schema="xenon",
    )
    op.create_index(
        "ix_futu_daily_statement_scope_date",
        "futu_daily_statement",
        ["broker", "account_env", "broker_account", "statement_date"],
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_futu_daily_statement_scope_date",
        table_name="futu_daily_statement",
        schema="xenon",
    )
    op.drop_table("futu_daily_statement", schema="xenon")
