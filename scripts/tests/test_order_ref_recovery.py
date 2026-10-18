"""PR-07 (OP-1): an UNKNOWN / PENDING place is bound back to IB by orderRef.

A place whose subprocess timed out or crashed leaves a row with no IB ids.
IB echoes ``orderRef = "xo:<submission_id>"`` on open orders and on
executions; the activity poller adopts the row from either source and the
permId-keyed mirror / fill recorder / sweep finish it. No evidence → the
row stays UNKNOWN (counted on /health). Nothing is ever re-sent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import insert, select

from xenon.api.services import ib_activity_mirror
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_submissions
from xenon.execution import orders_store
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU0000000")
NOW = datetime(2026, 6, 13, 14, 30, tzinfo=timezone.utc)
SID = "6f1c2d3e-0000-4000-8000-00000000a001"


def _seed(state: str = "UNKNOWN", sid: str = SID, quantity: int = 2) -> str:
    with get_sync_engine().begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=sid,
                user_id="u1",
                client_attempt_id=f"ca-{sid}",
                state=state,
                reason_code="PLACE_OUTCOME_UNKNOWN" if state == "UNKNOWN" else None,
                ticker="QQQ",
                security_type="STK",
                action="BUY",
                quantity=quantity,
                limit_price=Decimal("700.00"),
                tif="DAY",
                submitted_at=NOW,
                updated_at=NOW,
                modify_sequence=0,
                broker=SCOPE.broker,
                account_env=SCOPE.account_env,
                broker_account=SCOPE.broker_account,
            )
        )
    return sid


def _row(sid: str = SID) -> dict:
    with get_sync_engine().connect() as conn:
        return dict(
            conn.execute(select(order_submissions).where(order_submissions.c.submission_id == sid)).mappings().one()
        )


def _execution(sid: str = SID, *, shares: float = 2, account: str = "DU0000000") -> dict:
    return {
        "exec_id": "0000e1f2.6a1b2c3d.01.01",
        "perm_id": 555001,
        "ib_order_id": 77,
        "con_id": 320227571,
        "time": NOW,
        "symbol": "QQQ",
        "sec_type": "STK",
        "side": "BOT",
        "shares": shares,
        "price": 700.0,
        "exchange": "SMART",
        "commission": 0,
        "realized_pnl": 0,
        "commission_ready": False,
        "currency": None,
        "strike": None,
        "expiry": None,
        "right": None,
        "account": account,
        "order_ref": orders_store.order_ref_for(sid),
        "client_id": 31,
    }


def test_order_ref_round_trip():
    assert orders_store.submission_id_from_order_ref(orders_store.order_ref_for(SID)) == SID
    assert orders_store.submission_id_from_order_ref("leg_wizard:abc") is None
    assert orders_store.submission_id_from_order_ref("xo:") is None
    assert orders_store.submission_id_from_order_ref(None) is None


def test_fill_before_poll_recovered_to_filled_by_order_ref(pg_test_engine, monkeypatch):
    """The order filled before any poll saw it open: only the execution
    carries the orderRef. One tick adopts the row, links the fill, and the
    sweep marks it FILLED."""
    _seed()
    other_open = [{"permId": 999999, "orderId": 888888, "account": "DU0000000"}]
    monkeypatch.setattr(ib_activity_mirror, "_fetch_open_orders", lambda c: list(other_open))
    monkeypatch.setattr(ib_activity_mirror, "_fetch_ib_executions", lambda c, lookback_days=7: [_execution()])
    monkeypatch.setattr(
        ib_activity_mirror,
        "_sync_open_orders_to_postgres",
        lambda oo, *, scope, observed_at=None: {"registered": 0},
    )

    result = ib_activity_mirror.run_activity_poll_tick(ib_client_factory=lambda: object(), scope=SCOPE)

    assert result["order_ref_recovery"] == {"recovered": 1, "unresolved": 0}
    row = _row()
    assert (row["perm_id"], row["ib_order_id"], row["placing_client_id"]) == ("555001", "77", 31)
    assert row["state"] == "FILLED", result


def test_restart_recovery_from_open_order_adopts_row_without_duplicate(pg_test_engine):
    """After a restart (no in-memory state), an UNKNOWN row is matched to the
    still-open IB order by orderRef; the permId-keyed mirror then updates
    that row instead of inserting a snapshot-* duplicate."""
    _seed()
    open_order = {
        "orderId": 78,
        "permId": 555002,
        "clientId": 33,
        "orderRef": orders_store.order_ref_for(SID),
        "account": "DU0000000",
    }

    out = ib_activity_mirror.recover_by_order_ref([open_order], [], scope=SCOPE)

    assert out == {"recovered": 1, "unresolved": 0}
    row = _row()
    assert (row["state"], row["perm_id"], row["ib_order_id"], row["placing_client_id"]) == (
        "WORKING",
        "555002",
        "78",
        33,
    )
    assert row["reason_code"] is None
    mirrored = orders_store.register_from_snapshot(
        perm_id="555002",
        ib_order_id="78",
        ticker="QQQ",
        security_type="STK",
        action="BUY",
        quantity=2,
        limit_price=700.0,
        broker=SCOPE.broker,
        account_env=SCOPE.account_env,
        broker_account=SCOPE.broker_account,
        client_id=33,
    )
    assert mirrored["action"] != "INSERTED"
    with get_sync_engine().connect() as conn:
        assert conn.execute(select(order_submissions.c.submission_id)).scalars().all() == [SID]


def test_pending_row_recovered_too(pg_test_engine):
    _seed(state="PENDING")
    out = ib_activity_mirror.recover_by_order_ref([], [_execution()], scope=SCOPE)
    assert out["recovered"] == 1
    assert _row()["state"] == "WORKING"


def test_no_evidence_leaves_unknown_alone(pg_test_engine):
    _seed()
    foreign = _execution(account="DU9999999")
    out = ib_activity_mirror.recover_by_order_ref(
        [{"orderId": 5, "permId": 6, "orderRef": "leg_wizard:x", "account": "DU0000000"}],
        [foreign],
        scope=SCOPE,
    )
    assert out == {"recovered": 0, "unresolved": 1}
    row = _row()
    assert (row["state"], row["reason_code"], row["perm_id"]) == ("UNKNOWN", "PLACE_OUTCOME_UNKNOWN", None)


def test_terminal_row_is_never_adopted(pg_test_engine):
    _seed(state="CANCELLED")
    out = ib_activity_mirror.recover_by_order_ref([], [_execution()], scope=SCOPE)
    assert out == {"recovered": 0, "unresolved": 0}
    assert _row()["state"] == "CANCELLED"
