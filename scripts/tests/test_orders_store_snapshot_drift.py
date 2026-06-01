"""When TWS-side modifies change price/quantity on an order Xenon imported
from a snapshot, the next poll tick should mirror that change into Postgres
and emit an audit event.

Why this exists
---------------
Before this commit, `register_from_snapshot` was insert-only: existing rows
caused it to no-op silently. So if you opened an order in Xenon (or it got
imported from IB), then changed its price in TWS, our DB stayed at the old
price forever. The blotter, the order list, and `apply_modify_by_perm_id`
all worked off stale data.

Scope of the fix
----------------
Only `snapshot-*` rows get the UPDATE behavior. Xenon-authored UUID rows
keep their existing dedupe semantics — they have a `modify_sequence`
invariant that a TWS-side change would silently violate, and the right fix
for those is a separate (harder) policy decision. Documented as a known
gap in src/xenon/api/CLAUDE.md.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
# Phase 2 carve-out: this module's tests open their own SQLAlchemy engine
# (helpers calling sqlalchemy.create_engine directly, or subprocess CLIs)
# and therefore can't share the test's BEGIN/ROLLBACK transaction. They
# stay on Phase 1 TRUNCATE pre+post isolation via this marker. Migration
# to txn-rollback would require refactoring those local engine helpers to
# go through xenon.db.engine.get_sync_engine().
pytestmark = pytest.mark.committed_db

from sqlalchemy import create_engine, insert, select, text

from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_events, order_submissions
from xenon.execution.orders_store import init_store, register_from_snapshot


@pytest.fixture()
def db_path(tmp_path):
    path = tmp_path / "orders.duckdb"
    init_store(path)
    return path


def _events_for(submission_id: str) -> list[dict]:
    url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    ).replace("postgresql+asyncpg://", "postgresql+psycopg://")
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                select(order_events.c.kind, order_events.c.detail)
                .where(order_events.c.submission_id == submission_id)
                .order_by(order_events.c.at)
            ).all()
            return [{"kind": r[0], "detail": dict(r[1] or {})} for r in rows]
    finally:
        engine.dispose()


def _row_for(perm_id: str, *, scope: dict) -> dict | None:
    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(
                order_submissions.c.submission_id,
                order_submissions.c.limit_price,
                order_submissions.c.quantity,
                order_submissions.c.modify_sequence,
            ).where(
                order_submissions.c.perm_id == perm_id,
                order_submissions.c.broker == scope["broker"],
                order_submissions.c.account_env == scope["account_env"],
                order_submissions.c.broker_account == scope["broker_account"],
            )
        ).first()
    if row is None:
        return None
    return {
        "submission_id": row[0],
        "limit_price": float(row[1]) if row[1] is not None else None,
        "quantity": int(row[2]) if row[2] is not None else None,
        "modify_sequence": int(row[3]),
    }


SCOPE = {"broker": "IB", "account_env": "paper", "broker_account": "DU1"}


def test_returns_inserted_when_row_is_new(db_path):
    """First call inserts a snapshot row. New return shape: dict, action='INSERTED'."""
    result = register_from_snapshot(
        perm_id="2100001",
        ib_order_id="200",
        ticker="QQQ",
        security_type="STK",
        action="BUY",
        quantity=10,
        limit_price=500.0,
        **SCOPE,
    )
    assert isinstance(result, dict)
    assert result["action"] == "INSERTED"
    assert result.get("drift") is None


def test_returns_noop_when_values_unchanged(db_path):
    """Second call with identical values: no UPDATE, no event, action='NOOP'."""
    register_from_snapshot(
        perm_id="2100002",
        ib_order_id="201",
        ticker="QQQ",
        security_type="STK",
        action="BUY",
        quantity=10,
        limit_price=500.0,
        **SCOPE,
    )
    second = register_from_snapshot(
        perm_id="2100002",
        ib_order_id="201",
        ticker="QQQ",
        security_type="STK",
        action="BUY",
        quantity=10,
        limit_price=500.0,
        **SCOPE,
    )
    assert second["action"] == "NOOP"
    assert _events_for("snapshot-2100002") == []


def test_updates_snapshot_row_when_limit_price_drifts(db_path):
    """Real bug repro: user changed price in TWS, importer must mirror."""
    register_from_snapshot(
        perm_id="2100003",
        ib_order_id="202",
        ticker="SPX",
        security_type="BAG",
        action="SELL",
        quantity=11,
        limit_price=1.45,
        **SCOPE,
    )

    result = register_from_snapshot(
        perm_id="2100003",
        ib_order_id="202",
        ticker="SPX",
        security_type="BAG",
        action="SELL",
        quantity=11,
        limit_price=1.30,
        **SCOPE,
    )

    assert result["action"] == "UPDATED"
    drift = result["drift"]
    assert drift["limit_price"] == {"from": 1.45, "to": 1.30}
    assert "quantity" not in drift

    row = _row_for("2100003", scope=SCOPE)
    assert row["limit_price"] == 1.30
    assert row["submission_id"] == "snapshot-2100003"

    events = _events_for("snapshot-2100003")
    assert len(events) == 1
    assert events[0]["kind"] == "IB_MIRROR_UPDATE"
    assert events[0]["detail"]["limit_price"] == {"from": 1.45, "to": 1.30}


def test_updates_snapshot_row_when_quantity_drifts(db_path):
    """Quantity edit from TWS (rare but possible) must also mirror."""
    register_from_snapshot(
        perm_id="2100004",
        ib_order_id="203",
        ticker="QQQ",
        security_type="STK",
        action="BUY",
        quantity=10,
        limit_price=500.0,
        **SCOPE,
    )

    result = register_from_snapshot(
        perm_id="2100004",
        ib_order_id="203",
        ticker="QQQ",
        security_type="STK",
        action="BUY",
        quantity=15,
        limit_price=500.0,
        **SCOPE,
    )

    assert result["action"] == "UPDATED"
    assert result["drift"]["quantity"] == {"from": 10, "to": 15}
    assert "limit_price" not in result["drift"]

    row = _row_for("2100004", scope=SCOPE)
    assert row["quantity"] == 15


def test_updates_snapshot_row_when_both_price_and_qty_drift(db_path):
    """Both fields can change in a single TWS modify."""
    register_from_snapshot(
        perm_id="2100005",
        ib_order_id="204",
        ticker="QQQ",
        security_type="STK",
        action="BUY",
        quantity=10,
        limit_price=500.0,
        **SCOPE,
    )

    result = register_from_snapshot(
        perm_id="2100005",
        ib_order_id="204",
        ticker="QQQ",
        security_type="STK",
        action="BUY",
        quantity=20,
        limit_price=499.50,
        **SCOPE,
    )

    assert result["action"] == "UPDATED"
    assert result["drift"] == {
        "limit_price": {"from": 500.0, "to": 499.50},
        "quantity": {"from": 10, "to": 20},
    }


def test_float_precision_noise_does_not_trigger_drift(db_path):
    """Tribunal review caught: comparing raw floats with `!=` would spuriously
    detect drift on round-trip noise (e.g. 1.4500000001 vs 1.45 stored).
    With numeric(12,4) on the DB side, anything past 4dp is meaningless — round."""
    register_from_snapshot(
        perm_id="2100099",
        ib_order_id="299",
        ticker="SPX",
        security_type="BAG",
        action="SELL",
        quantity=11,
        limit_price=1.45,
        **SCOPE,
    )

    result = register_from_snapshot(
        perm_id="2100099",
        ib_order_id="299",
        ticker="SPX",
        security_type="BAG",
        action="SELL",
        quantity=11,
        limit_price=1.4500000000001,  # round-trip noise past 4dp
        **SCOPE,
    )

    assert result["action"] == "NOOP"
    assert _events_for("snapshot-2100099") == []


def test_does_not_update_uuid_authored_rows(db_path):
    """Xenon-authored UUID rows keep their dedupe semantics: no UPDATE,
    no event, action='SKIPPED_UUID'. Documented gap — see CLAUDE.md."""
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id="uuid-do-not-touch-001",
                user_id="local",
                client_attempt_id="attempt-1",
                ticker="QQQ",
                security_type="STK",
                action="BUY",
                quantity=10,
                multiplier=1,
                ib_order_id="42",
                perm_id="2100006",
                limit_price=500.0,
                state="WORKING",
                submitted_at=datetime(2026, 4, 29, tzinfo=timezone.utc),
                updated_at=datetime(2026, 4, 29, tzinfo=timezone.utc),
                modify_sequence=3,
                **SCOPE,
            )
        )

    result = register_from_snapshot(
        perm_id="2100006",
        ib_order_id="42",
        ticker="QQQ",
        security_type="STK",
        action="BUY",
        quantity=10,
        limit_price=499.0,  # would be drift if we touched UUID rows
        **SCOPE,
    )

    assert result["action"] == "SKIPPED_UUID"
    row = _row_for("2100006", scope=SCOPE)
    assert row["submission_id"] == "uuid-do-not-touch-001"
    assert row["limit_price"] == 500.0  # unchanged
    assert row["modify_sequence"] == 3  # unchanged
    assert _events_for("uuid-do-not-touch-001") == []
