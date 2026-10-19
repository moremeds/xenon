"""order_submissions: CHECK the state column against ORDER_STATES

Revision ID: 2026_10_18_order_state_check
Revises: 2026_10_16_scope_attempt_legs
Create Date: 2026-10-18

DATA-2 / OP-9: `order_submissions.state` was free text. This adds
`ck_order_sub_state CHECK (state IN (...))` with the list frozen from
`xenon.execution.orders_store.ORDER_STATES` (a test pins the two equal; a
migration must not change meaning when the code list grows later).

The upgrade FIRST lists any row whose state is not in the list and raises
with those states. It never rewrites, terminalizes or deletes a row:
PENDING / UNKNOWN stay valid; an unlisted state needs manual reconciliation
before a re-run. Prod inventory 2026-10-18: CANCELLED, FAILED, FILLED,
REJECTED, UNKNOWN, WORKING (all listed).

A plain ADD CONSTRAINT (validated immediately) instead of NOT VALID +
VALIDATE: the table holds about a hundred rows, so the validating scan
takes milliseconds under the ADD lock, and alembic runs the revision in one
transaction, where a VALIDATE in the same transaction would not shorten the
ACCESS EXCLUSIVE lock anyway. A validated constraint also proves the
existing rows, not only new writes. Lock acquisition is bounded by
`lock_timeout = 5s`: if a long transaction holds the table, the migration
fails fast (no order write waits behind it) and the operator re-runs it.

Downgrade drops only this constraint; no row changes.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2026_10_18_order_state_check"
down_revision: Union[str, Sequence[str], None] = "2026_10_16_scope_attempt_legs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ORDER_STATES = (
    "PENDING",
    "WORKING",
    "PARTIALLY_FILLED",
    "FILLED",
    "CANCELLED",
    "REJECTED",
    "FAILED",
    "UNKNOWN",
)
_NAME = "ck_order_sub_state"


def upgrade() -> None:
    conn = op.get_bind()
    # Bound every lock wait of this revision (the preflight SELECT and the
    # ADD CONSTRAINT, which needs ACCESS EXCLUSIVE): a long transaction
    # makes it fail fast (re-run the migrator) instead of queueing order
    # writes behind it. SET LOCAL ends with this migration's transaction.
    op.execute("SET LOCAL lock_timeout = '5s'")
    bad = conn.execute(
        sa.text(
            "SELECT state, count(*) FROM xenon.order_submissions "
            "WHERE state IS NULL OR NOT (state = ANY(:states)) GROUP BY state ORDER BY state"
        ),
        {"states": list(ORDER_STATES)},
    ).all()
    if bad:
        listed = ", ".join(f"{s!r} x{n}" for s, n in bad)
        raise RuntimeError(
            f"order_submissions holds states outside ORDER_STATES: {listed}. "
            "No row was changed; reconcile them by hand, then re-run the migration."
        )
    states_sql = ", ".join(f"'{s}'" for s in ORDER_STATES)
    op.create_check_constraint(_NAME, "order_submissions", f"state IN ({states_sql})", schema="xenon")


def downgrade() -> None:
    op.drop_constraint(_NAME, "order_submissions", schema="xenon", type_="check")
