"""Gate-4 PR-03b round 3 — amend sequencing (5), snapshot drift (6),
liveness (7) and adapter metadata (3) at the gate4 / orders_store layer."""

from __future__ import annotations

import os
import threading
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from scripts.tests.test_gate4_reserve import (
    SCOPE,
    _insert_submission,
    _reserve,
    _seed_snapshot,
    _snapshot_payload,
    _stock,
    _working_with_legs,
)
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_submissions
from xenon.execution import coverage, gate4, orders_store
from xenon.execution.orders_store import RequestRow


def _legs(sub_id):
    with get_sync_engine().begin() as conn:
        row = conn.execute(
            select(order_submissions.c.quantity, order_submissions.c.legs).where(
                order_submissions.c.submission_id == sub_id
            )
        ).first()
    return row[0], row[1]


def _set_perm(sub_id, perm):
    with get_sync_engine().begin() as conn:
        conn.execute(
            text("UPDATE xenon.order_submissions SET perm_id=:p WHERE submission_id=:s"), {"p": perm, "s": sub_id}
        )


def _mirror(perm, qty, legs=orders_store._UNSET):
    """What the IB mirror (sync pass / activity poller) writes for one order."""
    return orders_store.register_from_snapshot(perm, "4242", "SPY", "OPT", "SELL", qty, 5.0, 100, legs=legs, **SCOPE)


# ---------------------------------------------------------------------------
# item 5 — amend sequencing
# ---------------------------------------------------------------------------


def test_second_modify_refused_while_first_pending():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 300)]))
    sub_id = _working_with_legs("r3-seq-1", qty=1)
    first = gate4.begin_modify(sub_id, 2, SCOPE)
    assert first.accept

    second = gate4.begin_modify(sub_id, 1, SCOPE)  # decrease must not overwrite max
    assert not second.accept
    assert second.reasons == [gate4.MODIFY_PENDING_REASON]
    assert _legs(sub_id)[1]["pending_amend_qty"] == 2
    # A price-only modify is refused too — it would resend a quantity.
    assert gate4.begin_modify(sub_id, None, SCOPE).reasons == [gate4.MODIFY_PENDING_REASON]


@pytest.mark.parametrize("stale_outcome", ["rejected", "confirmed"])
def test_stale_settle_after_reconcile_is_noop(stale_outcome):
    """Review counterexample 2: amend A (1→2) is left pending; the mirror
    then sees qty 2 and clears it; amend B (2→1) opens. A's delayed reply
    must not touch B — neither release B's pending nor rewrite quantity."""
    _seed_snapshot(_snapshot_payload([_stock("SPY", 300)]))
    sub_id = _working_with_legs("r3-stale-1", qty=1)
    _set_perm(sub_id, "880001")
    a = gate4.begin_modify(sub_id, 2, SCOPE)
    assert a.accept
    assert _mirror("880001", 2)["reconciled"] is True
    assert _legs(sub_id)[0] == 2
    b = gate4.begin_modify(sub_id, 1, SCOPE)
    assert b.accept and b.amend_id != a.amend_id

    applied = gate4.settle_modify(sub_id, a.amend_id, stale_outcome, SCOPE, new_qty=2)

    assert applied is False
    qty, legs = _legs(sub_id)
    assert qty == 2
    assert legs["pending_amend_id"] == b.amend_id
    assert legs["pending_amend_qty"] == 1


def test_matching_settle_still_applies():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 300)]))
    sub_id = _working_with_legs("r3-match-1", qty=1)
    a = gate4.begin_modify(sub_id, 2, SCOPE)
    assert gate4.settle_modify(sub_id, a.amend_id, "confirmed", SCOPE, new_qty=2)
    qty, legs = _legs(sub_id)
    assert qty == 2 and legs["pending_amend_id"] is None


# ---------------------------------------------------------------------------
# item 6 — snapshot drift merges, never clears reservation state
# ---------------------------------------------------------------------------


def _snapshot_row_with_state(perm, *, pending_qty, guaranteed=True):
    payload = gate4.legs_payload_for(
        legs=[
            coverage.Leg(
                action="BUY",
                sym="SPY",
                sec="OPT",
                ratio=1,
                right="C",
                exp="20261218",
                mult=100,
                tc="SPY",
                strike=500.0,
                con_id=111,
                und_sec="STK",
            )
        ],
        guaranteed=guaranteed,
        account=SCOPE["broker_account"],
    )
    payload["pending_amend_qty"] = pending_qty
    payload["pending_amend_id"] = "amend-x"
    _mirror(perm, 1, legs=payload)
    return f"snapshot-{perm}"


def _observed(guaranteed=None):
    return gate4.legs_payload_for(
        legs=[
            coverage.Leg(
                action="BUY",
                sym="SPY",
                sec="OPT",
                ratio=1,
                right="C",
                exp="20261218",
                mult=100,
                tc="SPY",
                strike=500.0,
                con_id=111,
                und_sec="STK",
            )
        ],
        guaranteed=guaranteed,
        account=SCOPE["broker_account"],
    )


def test_drift_update_keeps_pending_and_guarantee_mode():
    sub_id = _snapshot_row_with_state("880101", pending_qty=3, guaranteed=True)
    # Price drift + an observation that carries no routing tags.
    out = orders_store.register_from_snapshot(
        "880101", "4242", "SPY", "OPT", "SELL", 1, 6.0, 100, legs=_observed(None), **SCOPE
    )
    assert out["action"] == "UPDATED"
    _, legs = _legs(sub_id)
    assert legs["pending_amend_qty"] == 3
    assert legs["pending_amend_id"] == "amend-x"
    assert legs["guaranteed"] is True


def test_drift_reconciles_pending_only_on_matching_quantity():
    sub_id = _snapshot_row_with_state("880201", pending_qty=3)
    orders_store.register_from_snapshot("880201", "4242", "SPY", "OPT", "SELL", 2, 5.0, 100, legs=_observed(), **SCOPE)
    assert _legs(sub_id)[1]["pending_amend_qty"] == 3
    orders_store.register_from_snapshot("880201", "4242", "SPY", "OPT", "SELL", 3, 5.0, 100, legs=_observed(), **SCOPE)
    qty, legs = _legs(sub_id)
    assert qty == 3 and legs["pending_amend_qty"] is None and legs["pending_amend_id"] is None


def test_uuid_row_reconciles_only_matching_quantity():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 300)]))
    sub_id = _working_with_legs("r3-uuid-1", qty=1)
    _set_perm(sub_id, "880301")
    assert gate4.begin_modify(sub_id, 2, SCOPE).accept

    assert _mirror("880301", 1) == {"action": "SKIPPED_UUID", "drift": None}
    assert _legs(sub_id)[1]["pending_amend_qty"] == 2
    assert _mirror("880301", 2)["reconciled"] is True
    qty, legs = _legs(sub_id)
    assert qty == 2 and legs["pending_amend_qty"] is None


@pytest.mark.committed_db
def test_mirror_legs_write_waits_for_account_lock():
    """register_from_snapshot takes the same account-scope advisory lock as
    the gate's check+insert — a legs write cannot interleave a reservation."""
    from sqlalchemy import create_engine

    sub_id = _snapshot_row_with_state("880401", pending_qty=None)
    holder = create_engine(os.environ["DATABASE_URL"])
    done = threading.Event()
    with holder.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": gate4.scope_lock_key(SCOPE)})
        t = threading.Thread(
            target=lambda: (
                orders_store.register_from_snapshot(
                    "880401", "4242", "SPY", "OPT", "SELL", 5, 5.0, 100, legs=_observed(), **SCOPE
                ),
                done.set(),
            )
        )
        t.start()
        assert not done.wait(1.0), "mirror write ran while the gate lock was held"
    assert done.wait(10.0)
    t.join()
    holder.dispose()
    assert _legs(sub_id)[0] == 5


# ---------------------------------------------------------------------------
# item 7 — liveness
# ---------------------------------------------------------------------------


def _reserve_qqq(attempt):
    body = {
        "type": "option",
        "symbol": "QQQ",
        "action": "SELL",
        "quantity": 1,
        "right": "C",
        "expiry": "2026-12-18",
        "strike": 400.0,
        "client_attempt_id": attempt,
    }
    meta = [{"con_id": 222, "tc": "QQQ", "mult": 100, "und_sec": "STK"}]
    cand, legs = gate4.candidate_from_place_body(body, SCOPE["broker_account"], meta)
    req = RequestRow(
        ticker="QQQ",
        security_type="OPT",
        action="SELL",
        quantity=1,
        expiry="2026-12-18",
        strike=Decimal("400"),
        right="C",
        multiplier=100,
        con_id=222,
        limit_price=Decimal("5"),
    )
    return gate4.reserve_with_coverage("u1", attempt, req, candidate=cand, legs_payload=legs, scope=SCOPE)


def test_terminal_legacy_bag_without_legs_does_not_block():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 100)]))
    _insert_submission("r3-dead-bag", state="CANCELLED", qty=1, sec="BAG", legs=None)
    assert _reserve("r3-after-dead").status == "winner"


def test_live_unresolved_bag_blocks_only_its_symbol():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 100), _stock("QQQ", 100)]))
    _insert_submission("r3-live-bag", state="WORKING", qty=1, sec="BAG", legs=None, ticker="QQQ")

    assert _reserve("r3-spy-ok").status == "winner"
    blocked = _reserve_qqq("r3-qqq-blocked")
    assert blocked.status == "rejected"
    assert any("order_unresolved_legs" in r for r in blocked.reasons)


def test_zero_quantity_row_is_ignored():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 100)]))
    _insert_submission("r3-zero", state="WORKING", qty=0, sec="BAG", legs=None)
    assert _reserve("r3-after-zero").status == "winner"


# ---------------------------------------------------------------------------
# item 3 — adapter metadata is never fabricated
# ---------------------------------------------------------------------------


def test_snapshot_order_without_adapter_metadata_stays_unqualified():
    order = {
        "account": "DU0000000",
        "contract": {
            "secType": "OPT",
            "symbol": "SPY",
            "right": "C",
            "expiry": "20261218",
            "strike": 500.0,
            "conId": 111,
        },
    }
    leg = gate4.legs_payload_from_snapshot_order(order)["legs"][0]
    assert (leg["tc"], leg["mult"], leg["und_sec"]) == (None, None, None)

    order["contract"].update(tradingClass="SPY", multiplier="100", underSecType="STK")
    leg = gate4.legs_payload_from_snapshot_order(order)["legs"][0]
    assert (leg["tc"], leg["mult"], leg["und_sec"]) == ("SPY", 100, "STK")
