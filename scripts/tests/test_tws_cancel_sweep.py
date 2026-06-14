"""TWS-side cancels must transition WORKING rows out of WORKING.

Decision table (api/CLAUDE.md § activity mirror):
  disappeared + fills cover quantity        -> FILLED   (first sweep)
  disappeared twice + fills incomplete      -> CANCELLED (reason TWS_CANCEL_MIRROR)
  disappeared once then reappears           -> stays WORKING, grace cleared
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import insert, select

from xenon.api.services.ib_activity_mirror import sweep_disappeared_orders
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_fills, order_submissions
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU0000000")
NOW = datetime(2026, 6, 13, 14, 30, tzinfo=timezone.utc)


def _seed_working(perm_id: str, *, quantity: int = 2, ib_order_id: str = "0") -> str:
    submission_id = f"snapshot-{perm_id}"
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=submission_id,
                user_id="snapshot",
                client_attempt_id=f"ca-{perm_id}",
                state="WORKING",
                ticker="QQQ",
                security_type="STK",
                action="BUY",
                quantity=quantity,
                limit_price=Decimal("700.00"),
                tif="DAY",
                perm_id=perm_id,
                ib_order_id=ib_order_id,
                submitted_at=NOW,
                updated_at=NOW,
                modify_sequence=0,
                broker=SCOPE.broker,
                account_env=SCOPE.account_env,
                broker_account=SCOPE.broker_account,
            )
        )
    return submission_id


def _seed_fill(perm_id: str, *, qty: str, exec_id: str) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_fills).values(
                exec_id=exec_id,
                submission_id=f"snapshot-{perm_id}",
                perm_id=perm_id,
                ticker="QQQ",
                side="BUY",
                qty=Decimal(qty),
                price=Decimal("700.00"),
                commission=Decimal("0.35"),
                filled_at=NOW,
                metadata={"sec_type": "STK", "legacy_source": "test"},
                broker=SCOPE.broker,
                account_env=SCOPE.account_env,
                broker_account=SCOPE.broker_account,
            )
        )


def _state_of(submission_id: str) -> str:
    engine = get_sync_engine()
    with engine.connect() as conn:
        return conn.execute(
            select(order_submissions.c.state).where(order_submissions.c.submission_id == submission_id)
        ).scalar_one()


# A non-empty snapshot of some *other* still-open order. Every "target is
# absent" test passes this so the target's disappearance is real, not an
# empty-snapshot artifact (the empty-snapshot path is its own test).
OTHER = [{"permId": "999999", "orderId": "888888"}]


def test_disappeared_with_full_fills_marks_filled_first_sweep() -> None:
    sid = _seed_working("9001", quantity=2)
    _seed_fill("9001", qty="2", exec_id="sweep-fill-1")
    grace: set[str] = set()
    sweep_disappeared_orders(OTHER, scope=SCOPE, grace=grace)
    assert _state_of(sid) == "FILLED"
    assert sid not in grace


def test_disappeared_without_fills_cancels_on_second_sweep() -> None:
    sid = _seed_working("9002")
    grace: set[str] = set()
    sweep_disappeared_orders(OTHER, scope=SCOPE, grace=grace)
    assert _state_of(sid) == "WORKING"  # first sweep: grace only
    assert sid in grace
    sweep_disappeared_orders(OTHER, scope=SCOPE, grace=grace)
    assert _state_of(sid) == "CANCELLED"  # second sweep: confirmed gone


def test_reappearing_order_clears_grace() -> None:
    sid = _seed_working("9003")
    grace: set[str] = set()
    sweep_disappeared_orders(OTHER, scope=SCOPE, grace=grace)
    assert sid in grace
    sweep_disappeared_orders(OTHER + [{"permId": "9003"}], scope=SCOPE, grace=grace)
    assert _state_of(sid) == "WORKING"
    assert sid not in grace


def test_empty_snapshot_never_cancels() -> None:
    """An empty open-order snapshot must not cancel working rows — it is a
    post-reconnect/stale-read signature, not 'everything cancelled'."""
    sid = _seed_working("9004")
    grace: set[str] = set()
    result = sweep_disappeared_orders([], scope=SCOPE, grace=grace)
    assert result.get("skipped") == "empty_snapshot"
    assert _state_of(sid) == "WORKING"
    # Even a second empty sweep does not cancel.
    sweep_disappeared_orders([], scope=SCOPE, grace=grace)
    assert _state_of(sid) == "WORKING"


def test_present_by_order_id_survives_permid_race() -> None:
    """An order still open but reported with permId=0 (the documented
    client-side race) must be matched by ib_order_id, not cancelled."""
    sid = _seed_working("9005", ib_order_id="55")
    grace: set[str] = set()
    # Snapshot has the order under orderId=55 with permId 0 (race).
    sweep_disappeared_orders([{"permId": 0, "orderId": "55"}], scope=SCOPE, grace=grace)
    assert _state_of(sid) == "WORKING"
    assert sid not in grace
