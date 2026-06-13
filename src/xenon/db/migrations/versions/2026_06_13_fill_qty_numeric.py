"""order_fills.qty and trades.quantity: Integer -> Numeric(20, 8)

Fractional-share executions (recurring QQQ/SPY buys) were truncated to
qty=0 by the Integer column.

Revision ID: 2026_06_13_fill_qty_numeric
Revises: 58107314f4c9
Create Date: 2026-06-13

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2026_06_13_fill_qty_numeric"
# Single committed head on master (see plan Task 4 Step 2). If a fresh head
# computation prints multiple heads, make this a tuple of them.
down_revision: Union[str, Sequence[str], None] = "58107314f4c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "order_fills",
        "qty",
        type_=sa.Numeric(20, 8),
        existing_type=sa.Integer(),
        existing_nullable=False,
        schema="xenon",
    )
    op.alter_column(
        "trades",
        "quantity",
        type_=sa.Numeric(20, 8),
        existing_type=sa.Integer(),
        existing_nullable=False,
        schema="xenon",
    )


def downgrade() -> None:
    op.alter_column(
        "trades",
        "quantity",
        type_=sa.Integer(),
        existing_type=sa.Numeric(20, 8),
        existing_nullable=False,
        schema="xenon",
    )
    op.alter_column(
        "order_fills",
        "qty",
        type_=sa.Integer(),
        existing_type=sa.Numeric(20, 8),
        existing_nullable=False,
        schema="xenon",
    )
