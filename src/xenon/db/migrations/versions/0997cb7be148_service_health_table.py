"""service_health table

Revision ID: 0997cb7be148
Revises: 2026_06_13_fill_qty_numeric
Create Date: 2026-06-15 13:13:57.815081

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0997cb7be148"
down_revision: Union[str, Sequence[str], None] = "2026_06_13_fill_qty_numeric"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "service_health",
        sa.Column("service", sa.Text(), nullable=False),
        sa.Column("broker", sa.Text(), nullable=False),
        sa.Column("account_env", sa.Text(), nullable=False),
        sa.Column("broker_account", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("service", "broker", "account_env", "broker_account", name="pk_service_health"),
        schema="xenon",
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("service_health", schema="xenon")
