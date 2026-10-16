"""order_submissions: account-scoped attempt key + durable legs payload

Revision ID: 2026_10_16_scope_attempt_legs
Revises: 2026_06_22_positions_currency
Create Date: 2026-10-16

Spec §2.3/§2.5: idempotency becomes account-scoped — the unique key drops
`user_id` and becomes (broker, account_env, broker_account,
client_attempt_id). `legs jsonb NULL` stores the effective-leg reservation
payload (actions, ratios, conIds, multiplier, guarantee mode, original and
replacement qty) so demand survives restarts.

The upgrade FIRST inventories rows that would collide under the new key and
raises with the group count — order evidence is never deleted or merged;
collisions need manual reconciliation before re-running.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "2026_10_16_scope_attempt_legs"
down_revision: Union[str, Sequence[str], None] = "2026_06_22_positions_currency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    collisions = conn.execute(
        sa.text(
            """
            SELECT count(*) FROM (
                SELECT 1
                FROM xenon.order_submissions
                WHERE client_attempt_id IS NOT NULL
                GROUP BY broker, account_env, broker_account, client_attempt_id
                HAVING count(*) > 1
            ) colliding
            """
        )
    ).scalar()
    if collisions:
        raise RuntimeError(
            "order_submissions scope-attempt rekey blocked: "
            f"{collisions} (broker, account_env, broker_account, "
            "client_attempt_id) group(s) hold more than one row. No rows "
            "were deleted or merged — reconcile manually and re-run."
        )

    op.add_column(
        "order_submissions",
        sa.Column("legs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        schema="xenon",
    )
    op.drop_constraint("uq_order_sub_user_attempt", "order_submissions", schema="xenon")
    op.create_unique_constraint(
        "uq_order_sub_scope_attempt",
        "order_submissions",
        ["broker", "account_env", "broker_account", "client_attempt_id"],
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_constraint("uq_order_sub_scope_attempt", "order_submissions", schema="xenon")
    op.create_unique_constraint(
        "uq_order_sub_user_attempt",
        "order_submissions",
        ["broker", "account_env", "broker_account", "user_id", "client_attempt_id"],
        schema="xenon",
    )
    op.drop_column("order_submissions", "legs", schema="xenon")
