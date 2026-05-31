"""add nav_history one_env_per_day unique index

Revision ID: 489476c351cc
Revises: 260fabba18d6
Create Date: 2026-06-01 01:18:29.358326

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '489476c351cc'
down_revision: Union[str, Sequence[str], None] = '260fabba18d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Atomic dual-curve protection (Decisions §13).

    Excludes `account_env` from the unique columns — two rows with different
    account_env values cannot coexist for the same (broker, broker_account, date).
    The existing PK (broker, account_env, broker_account, date) is preserved.
    """
    op.execute(
        "CREATE UNIQUE INDEX nav_history_one_env_per_day "
        "ON xenon.nav_history (broker, broker_account, date)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS xenon.nav_history_one_env_per_day")
