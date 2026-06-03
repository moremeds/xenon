"""futu statement raw inbox — keyed on source_uid, stores raw PDF for re-parse

Revision ID: 2026_06_03_futu_inbox
Revises: 2026_06_03_futu_stmt2
Create Date: 2026-06-03

Background
----------
The typed table `futu_daily_statement` only accepts rows where every parsed
field could be extracted. Older Futu daily-statement PDFs use a different
section-header convention (no "Ending Assets Overview" line), so a large
fraction of historical statements fail current parser heuristics.

This inbox table is the catch-all for raw bytes we couldn't (yet) parse —
PK on `source_uid` so re-running the IMAP sync is idempotent. When the
parser learns the older layout, a follow-up job can read these rows,
parse, and INSERT into `futu_daily_statement`.

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "2026_06_03_futu_inbox"
down_revision: Union[str, Sequence[str], None] = "2026_06_03_futu_stmt2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "futu_statement_inbox",
        sa.Column("broker", sa.Text(), primary_key=True),
        sa.Column("account_env", sa.Text(), primary_key=True),
        sa.Column("broker_account", sa.Text(), primary_key=True),
        sa.Column("source_uid", sa.Text(), primary_key=True),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("sender", sa.Text(), nullable=True),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("attachment_name", sa.Text(), nullable=True),
        sa.Column("raw_pdf", sa.LargeBinary(), nullable=False),
        sa.Column("parse_error", sa.Text(), nullable=True),
        sa.Column(
            "ingested_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("broker = 'FUTU'", name="ck_futu_statement_inbox_broker"),
        sa.CheckConstraint(
            "account_env IN ('paper', 'live', 'sim')",
            name="ck_futu_statement_inbox_account_env",
        ),
        schema="xenon",
    )
    op.create_index(
        "ix_futu_statement_inbox_scope_received",
        "futu_statement_inbox",
        ["broker", "account_env", "broker_account", "received_at"],
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_futu_statement_inbox_scope_received",
        table_name="futu_statement_inbox",
        schema="xenon",
    )
    op.drop_table("futu_statement_inbox", schema="xenon")
