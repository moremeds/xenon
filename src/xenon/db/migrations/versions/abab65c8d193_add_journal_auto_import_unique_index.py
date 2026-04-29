"""add journal auto import unique index

Revision ID: abab65c8d193
Revises: 2e8d6c8a19b4
Create Date: 2026-04-29 13:23:00.845724

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "abab65c8d193"
down_revision: Union[str, Sequence[str], None] = "2e8d6c8a19b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        "uq_journal_auto_import",
        "journal_entries",
        ["broker", "account_env", "broker_account", "trade_id"],
        unique=True,
        schema="xenon",
        postgresql_where=sa.text("decision = 'IB_AUTO_IMPORT' AND trade_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_journal_auto_import", table_name="journal_entries", schema="xenon")
