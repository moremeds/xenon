"""record_external_fills must link fills back to the submission they belong to.

Why this exists
---------------
The blotter UI reads xenon.trades. Trades are derived from xenon.order_fills
via aggregate_trade_from_fills. The aggregator has two grouping paths:

  - aggregate_trade_from_fills(submission_id=...) → groups by the actual
    order. This is what we want for any fill whose origin we know.
  - aggregate_trade_from_fills(legacy_id=...) → orphan fallback that hashes
    the contract key. Used only when the fill came from outside Xenon and
    we have no submission to point to.

Before this commit, record_external_fills always passed submission_id=None
and only ever used the legacy_id path — even for orders Xenon imported via
register_from_snapshot, where the submission row exists and the permId is
known. Result: fills got grouped under a synthetic legacy hash that didn't
match the snapshot row, so the blotter never tied them together.

Fix: when the fill's permId matches an order_submissions row in the same
scope, record the fill against that submission and aggregate by it. Fall
back to the legacy_id path only for true orphans (TWS-placed orders
Xenon never imported, or pre-scope rows).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select

from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_fills, order_submissions
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU0000000")


def _execution(*, perm_id: str, exec_id: str = "x-1", price: float = 1.45) -> dict:
    return {
        "exec_id": exec_id,
        "perm_id": perm_id,
        "ib_order_id": "200",
        "con_id": 12345,
        "time": datetime(2026, 4, 29, 21, 35, tzinfo=timezone.utc),
        "symbol": "SPX",
        "sec_type": "BAG",
        "side": "SLD",
        "shares": 11,
        "price": price,
        "exchange": "SMART",
        "commission": 1.50,
        "realized_pnl": 0.0,
        "strike": None,
        "expiry": None,
        "right": None,
    }


def _seed_snapshot_row(perm_id: str, *, scope: AccountScope = SCOPE) -> str:
    """Seed a snapshot-imported row that a fill should resolve to."""
    submission_id = f"snapshot-{perm_id}"
    engine = get_sync_engine()
    now = datetime(2026, 4, 29, 18, 22, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=submission_id,
                user_id="snapshot",
                client_attempt_id=submission_id,
                ticker="SPX",
                security_type="BAG",
                action="SELL",
                quantity=11,
                multiplier=100,
                ib_order_id="200",
                perm_id=perm_id,
                limit_price=1.45,
                state="WORKING",
                submitted_at=now,
                updated_at=now,
                modify_sequence=0,
                broker=scope.broker,
                account_env=scope.account_env,
                broker_account=scope.broker_account,
            )
        )
    return submission_id


def _seed_order_id_only_row(ib_order_id: str, *, scope: AccountScope = SCOPE) -> str:
    """Seed a Xenon-authored row whose submit ack did not yet have permId."""
    submission_id = f"uuid-order-{ib_order_id}"
    engine = get_sync_engine()
    now = datetime(2026, 5, 7, 16, 16, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=submission_id,
                user_id="local",
                client_attempt_id=f"attempt-{ib_order_id}",
                ticker="SPY",
                security_type="OPT",
                action="BUY",
                quantity=1,
                multiplier=100,
                ib_order_id=ib_order_id,
                perm_id="",
                limit_price=0,
                state="WORKING",
                submitted_at=now,
                updated_at=now,
                modify_sequence=0,
                broker=scope.broker,
                account_env=scope.account_env,
                broker_account=scope.broker_account,
            )
        )
    return submission_id


def test_fill_links_to_existing_snapshot_submission():
    """Real bug repro: SPX combo filled in TWS; xenon.order_fills row should
    point to the snapshot submission so the blotter can join them."""
    from xenon.execution.ib_reconcile import record_external_fills

    submission_id = _seed_snapshot_row("9300001")
    result = record_external_fills([_execution(perm_id="9300001", exec_id="ex-9300001")], scope=SCOPE)

    assert result["inserted"] == 1

    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(
                order_fills.c.exec_id,
                order_fills.c.submission_id,
                order_fills.c.perm_id,
            ).where(order_fills.c.exec_id == "ex-9300001")
        ).first()

    assert row is not None
    assert row.submission_id == submission_id
    assert row.perm_id == "9300001"


def test_fill_falls_back_to_ib_order_id_when_submit_ack_perm_id_was_zero():
    """MKT orders can return permId=0 from /orders/place, then fill with a
    real permId seconds later. The fill must still link to the Xenon
    submission by ib_order_id and backfill the real permId onto the row."""
    from xenon.execution.ib_reconcile import record_external_fills

    submission_id = _seed_order_id_only_row("21")
    result = record_external_fills(
        [
            _execution(perm_id="789792141", exec_id="ex-order-id-fallback")
            | {"ib_order_id": "21", "symbol": "SPY", "sec_type": "OPT"}
        ],
        scope=SCOPE,
    )

    assert result["inserted"] == 1

    engine = get_sync_engine()
    with engine.connect() as conn:
        fill = conn.execute(
            select(order_fills.c.submission_id, order_fills.c.perm_id).where(
                order_fills.c.exec_id == "ex-order-id-fallback"
            )
        ).one()
        submission = conn.execute(
            select(order_submissions.c.perm_id).where(order_submissions.c.submission_id == submission_id)
        ).one()

    assert fill.submission_id == submission_id
    assert fill.perm_id == "789792141"
    assert submission.perm_id == "789792141"


def test_order_id_fallback_ignores_reused_order_id_with_existing_perm_id():
    """IB orderId is scoped to a client session and can be reused. A later
    external fill must not attach to an unrelated row that already has its
    own real permId just because the numeric orderId matches."""
    from xenon.execution.ib_reconcile import record_external_fills

    engine = get_sync_engine()
    now = datetime(2026, 5, 8, 0, 15, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(order_fills.delete().where(order_fills.c.exec_id == "ex-reused-order-id"))
        conn.execute(
            order_submissions.delete().where(order_submissions.c.submission_id == "existing-perm-order-4")
        )
        conn.execute(
            insert(order_submissions).values(
                submission_id="existing-perm-order-4",
                user_id="local",
                client_attempt_id="paper-smoke-existing-order-4",
                ticker="SPY",
                security_type="OPT",
                action="BUY",
                quantity=1,
                multiplier=100,
                ib_order_id="4",
                perm_id="789792140",
                limit_price=0.01,
                state="WORKING",
                submitted_at=now,
                updated_at=now,
                modify_sequence=0,
                broker=SCOPE.broker,
                account_env=SCOPE.account_env,
                broker_account=SCOPE.broker_account,
            )
        )

    result = record_external_fills(
        [
            _execution(perm_id="789792288", exec_id="ex-reused-order-id")
            | {"ib_order_id": "4", "symbol": "GM", "sec_type": "STK", "price": 78.9}
        ],
        scope=SCOPE,
    )

    assert result["inserted"] == 1
    assert len(result["affected_legacy_ids"]) == 1

    with engine.connect() as conn:
        fill = conn.execute(
            select(order_fills.c.submission_id).where(order_fills.c.exec_id == "ex-reused-order-id")
        ).one()
        submission = conn.execute(
            select(order_submissions.c.perm_id).where(order_submissions.c.submission_id == "existing-perm-order-4")
        ).one()

    assert fill.submission_id is None
    assert submission.perm_id == "789792140"


def test_orphan_fill_with_no_matching_submission_keeps_legacy_id_grouping():
    """A fill whose permId doesn't match any imported order is still
    recorded — submission_id stays None, aggregation falls back to legacy_id."""
    from xenon.execution.ib_reconcile import record_external_fills

    # Note: we deliberately do NOT seed a submission row.
    result = record_external_fills([_execution(perm_id="9300002", exec_id="ex-9300002")], scope=SCOPE)

    assert result["inserted"] == 1
    assert len(result["affected_legacy_ids"]) == 1

    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(select(order_fills.c.submission_id).where(order_fills.c.exec_id == "ex-9300002")).first()

    assert row is not None
    assert row.submission_id is None  # orphan path


def test_resolution_is_scope_aware():
    """A snapshot row with the same permId in a different scope must NOT
    capture the fill. Paper/live cannot blend in the shared Postgres."""
    from xenon.execution.ib_reconcile import record_external_fills

    other_scope = AccountScope(broker="IB", account_env="live", broker_account="U18007831")
    _seed_snapshot_row("9300003", scope=other_scope)

    result = record_external_fills([_execution(perm_id="9300003", exec_id="ex-9300003")], scope=SCOPE)
    assert result["inserted"] == 1

    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(order_fills.c.submission_id, order_fills.c.broker_account).where(
                order_fills.c.exec_id == "ex-9300003"
            )
        ).first()

    assert row.submission_id is None  # different scope — must not link
    assert row.broker_account == "DU0000000"


def test_aggregator_called_with_submission_id_when_matched(monkeypatch):
    """When a fill links to a submission, aggregate by submission_id (the
    primary path), not by legacy_id (the orphan fallback)."""
    from xenon.execution import ib_reconcile

    _seed_snapshot_row("9300004")

    captured: list[dict] = []

    def fake_aggregate(*, submission_id=None, legacy_id=None, **_kwargs):
        captured.append({"submission_id": submission_id, "legacy_id": legacy_id})

    monkeypatch.setattr(ib_reconcile, "aggregate_trade_from_fills", fake_aggregate)
    ib_reconcile.record_external_fills(
        [_execution(perm_id="9300004", exec_id="ex-9300004")],
        scope=SCOPE,
    )

    assert len(captured) == 1
    assert captured[0]["submission_id"] == "snapshot-9300004"
    assert captured[0]["legacy_id"] is None


def test_aggregator_called_with_legacy_id_when_orphan(monkeypatch):
    """Orphans still use legacy_id grouping (preserves existing behavior)."""
    from xenon.execution import ib_reconcile

    captured: list[dict] = []

    def fake_aggregate(*, submission_id=None, legacy_id=None, **_kwargs):
        captured.append({"submission_id": submission_id, "legacy_id": legacy_id})

    monkeypatch.setattr(ib_reconcile, "aggregate_trade_from_fills", fake_aggregate)
    ib_reconcile.record_external_fills(
        [_execution(perm_id="9300005", exec_id="ex-9300005")],
        scope=SCOPE,
    )

    assert len(captured) == 1
    assert captured[0]["submission_id"] is None
    assert captured[0]["legacy_id"] is not None
