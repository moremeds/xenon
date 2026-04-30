"""add tif column to order_submissions

Revision ID: 8a3f2c7d1e90
Revises: 4da18cde403f
Create Date: 2026-04-30 09:30:00.000000

The /orders endpoint hardcoded tif='DAY' so a GTC placed in TWS rendered
as DAY in the Open Orders panel. Adding a first-class column lets the
activity poller persist the IB-reported tif and the route surface it.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "8a3f2c7d1e90"
down_revision: Union[str, Sequence[str], None] = "4da18cde403f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "order_submissions",
        sa.Column("tif", sa.Text(), nullable=False, server_default="DAY"),
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_column("order_submissions", "tif", schema="xenon")
