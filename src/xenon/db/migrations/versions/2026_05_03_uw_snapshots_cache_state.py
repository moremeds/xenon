"""extend uw_analyze_snapshots with internal cache state columns

Revision ID: 9f2c4a1d8e57
Revises: 7c1e3a9b2f01
Create Date: 2026-05-03 17:30:00.000000

PG cutoff fix-up: rehydrating ``UwAnalyzeCache`` from the latest snapshot per
ticker was dropping three pieces of internal state that lived only in the
in-memory entry shape and the (now-deleted) JSON cache file:

- ``sources`` — list of where the entry was learned about (portfolio,
  watchlist, ad-hoc). Used by source-aware eviction priority. Without it,
  every restarted entry lands in tier 0 (adhoc) and gets evicted under
  pressure regardless of importance.
- ``oi_baseline`` — open-interest snapshot used to gate the OI re-fetch on
  the next refresh. Without it, every restart re-pays the OI fetch cost.
- ``previous_snapshot`` — prior ``current`` value used by the diff
  computation. Without it, the first refresh after restart can't emit
  prev-vs-current materialized changes for that ticker.

All three are nullable so existing rows (pre-cutoff) work unchanged; the
cache writer fills them in on the next refresh.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "9f2c4a1d8e57"
down_revision: Union[str, Sequence[str], None] = "7c1e3a9b2f01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "uw_analyze_snapshots",
        sa.Column("sources", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema="xenon",
    )
    op.add_column(
        "uw_analyze_snapshots",
        sa.Column("oi_baseline", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema="xenon",
    )
    op.add_column(
        "uw_analyze_snapshots",
        sa.Column("previous_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_column("uw_analyze_snapshots", "previous_snapshot", schema="xenon")
    op.drop_column("uw_analyze_snapshots", "oi_baseline", schema="xenon")
    op.drop_column("uw_analyze_snapshots", "sources", schema="xenon")
