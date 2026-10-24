"""order_submissions: allow broker FUTU, add broker_order_id

Revision ID: 2026_10_24_futu_order_sub
Revises: 2026_10_20_drop_signal_tables
Create Date: 2026-10-24

Futu orders X3a (additive; no writer ships with it):

- `ck_order_sub_broker_ib_only` (broker = 'IB') is replaced by
  `ck_order_sub_broker` (broker IN ('IB','FUTU')), the same shape as
  `ck_fills_broker` on order_fills. The old name would be false once FUTU is
  allowed, so it is renamed.
- New nullable column `broker_order_id TEXT` (the broker's own order id as a
  string; Futu order ids do not fit `ib_order_id` / `perm_id` semantics).
- New partial unique index `uq_order_sub_broker_order_id` on
  (broker, account_env, broker_account, broker_order_id)
  WHERE broker_order_id IS NOT NULL: one row per broker order per scope.

Not touched: trades and the wizard tables stay IB-only; order_fills already
allows FUTU (`ck_fills_broker`); every existing unique constraint and index
on order_submissions is unchanged. No row is changed.

Lock waits are bounded by `lock_timeout = 5s` (same as
2026_10_18_order_state_check): a long transaction on the table makes the
migration fail fast; re-run the migrator.

Downgrade restores `ck_order_sub_broker_ib_only` and drops the index and the
column. It never deletes or rewrites rows: it first refuses (raises, nothing
changed) when any FUTU row exists or any row has a broker_order_id, because
the IB-only CHECK would reject those rows or the column drop would lose data.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2026_10_24_futu_order_sub"
down_revision: Union[str, Sequence[str], None] = "2026_10_20_drop_signal_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "order_submissions"
_OLD_CK = "ck_order_sub_broker_ib_only"
_NEW_CK = "ck_order_sub_broker"
_INDEX = "uq_order_sub_broker_order_id"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint(_OLD_CK, _TABLE, schema="xenon", type_="check")
    op.create_check_constraint(_NEW_CK, _TABLE, "broker IN ('IB','FUTU')", schema="xenon")
    op.add_column(_TABLE, sa.Column("broker_order_id", sa.Text(), nullable=True), schema="xenon")
    op.create_index(
        _INDEX,
        _TABLE,
        ["broker", "account_env", "broker_account", "broker_order_id"],
        unique=True,
        schema="xenon",
        postgresql_where=sa.text("broker_order_id IS NOT NULL"),
    )


def downgrade() -> None:
    conn = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '5s'")
    futu, with_id = conn.execute(
        sa.text(
            "SELECT count(*) FILTER (WHERE broker <> 'IB'), "
            "count(*) FILTER (WHERE broker_order_id IS NOT NULL) FROM xenon.order_submissions"
        )
    ).one()
    if futu or with_id:
        raise RuntimeError(
            f"order_submissions holds {futu} non-IB row(s) and {with_id} row(s) with broker_order_id. "
            "No row was changed; the downgrade would reject or lose them. Reconcile them first."
        )
    op.drop_index(_INDEX, table_name=_TABLE, schema="xenon")
    op.drop_column(_TABLE, "broker_order_id", schema="xenon")
    op.drop_constraint(_NEW_CK, _TABLE, schema="xenon", type_="check")
    op.create_check_constraint(_OLD_CK, _TABLE, "broker = 'IB'", schema="xenon")
