"""rename uw_snapshots to uw_analyze_snapshots

Revision ID: 80e181cf1308
Revises: 9b645325b50d
Create Date: 2026-04-26 10:49:43.724671

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "80e181cf1308"
down_revision: Union[str, Sequence[str], None] = "9b645325b50d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table("uw_snapshots", "uw_analyze_snapshots", schema="xenon")
    op.execute("ALTER INDEX xenon.ix_uw_snap_ticker_time RENAME TO ix_uw_analyze_snap_ticker_time")


def downgrade() -> None:
    op.execute("ALTER INDEX xenon.ix_uw_analyze_snap_ticker_time RENAME TO ix_uw_snap_ticker_time")
    op.rename_table("uw_analyze_snapshots", "uw_snapshots", schema="xenon")
