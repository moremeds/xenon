"""add account_snapshots.payload jsonb column

Restores the IB portfolio read path after the postgres-migration left the UI
reading a stale data/portfolio.json. The structured payload (kelly,
structure_type, account_summary detail) is computed at sync time and now
persists alongside the flat columns so the new GET /portfolio endpoint can
return the same shape the UI's PortfolioDataSchema expects.

See docs/plans/2026-04-27-portfolio-postgres-read-path.md.

Revision ID: 4f2a1c9e8b3d
Revises: 008128225148
Create Date: 2026-04-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4f2a1c9e8b3d"
down_revision: Union[str, Sequence[str], None] = "008128225148"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "account_snapshots",
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_column("account_snapshots", "payload", schema="xenon")
