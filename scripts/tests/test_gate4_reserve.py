"""Gate-4 atomic reservation tests (PR-03b).

`gate4.reserve_with_coverage` must evaluate `coverage.evaluate` and insert the
`order_submissions` row in ONE transaction serialized by an account-scope
`pg_advisory_xact_lock`. These tests seed `account_snapshots` (with the
sync-pass `observation` stamp), `order_submissions`, and `order_fills`
directly, then drive the real functions against Postgres.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import insert, select, text

from xenon.db.engine import get_sync_engine
from xenon.db.schema import account_snapshots, order_fills, order_submissions
from xenon.execution import coverage, gate4
from xenon.execution.orders_store import RequestRow

SCOPE = {"broker": "IB", "account_env": "paper", "broker_account": "DU0000000"}
NOW = datetime.now(timezone.utc)


def _snapshot_payload(positions, *, as_of=None, complete=True, account="DU0000000"):
    return {
        "positions": positions,
        "available_funds": 0,
        "observation": {
            "as_of": (as_of or NOW).isoformat(),
            "complete": complete,
            "account": account,
            "reads": {"positions": True, "open_orders": True, "executions": True},
        },
    }


def _stock(sym, qty):
    return {
        "ticker": sym,
        "expiry": None,
        "legs": [{"direction": "LONG" if qty >= 0 else "SHORT", "type": "Stock", "contracts": abs(qty), "strike": 0}],
    }


def _opt_leg_pos(sym, qty, *, right="C", strike=500, exp="2026-12-18"):
    return {
        "ticker": sym,
        "expiry": exp,
        "legs": [
            {
                "direction": "LONG" if qty >= 0 else "SHORT",
                "type": "Call" if right == "C" else "Put",
                "contracts": abs(qty),
                "strike": strike,
            }
        ],
    }


def _seed_snapshot(payload, *, snapshot_at=None):
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(account_snapshots).values(
                account="DU0000000",
                bankroll=Decimal("0"),
                payload=json.loads(json.dumps(payload)),
                snapshot_at=snapshot_at or NOW,
                **SCOPE,
            )
        )


def _sell_call_req(qty=1, ticker="SPY"):
    return RequestRow(
        ticker=ticker,
        security_type="OPT",
        action="SELL",
        quantity=qty,
        expiry="2026-12-18",
        strike=Decimal("500"),
        right="C",
        multiplier=100,
        con_id=111,
        limit_price=Decimal("5.0"),
    )


def _sell_call_candidate(qty=1, attempt="cand-1"):
    body = {
        "type": "option",
        "symbol": "SPY",
        "action": "SELL",
        "quantity": qty,
        "right": "C",
        "expiry": "2026-12-18",
        "strike": 500.0,
        "client_attempt_id": attempt,
    }
    return gate4.candidate_from_place_body(body, SCOPE["broker_account"])


def _reserve(attempt, body=None, req=None):
    body = body or {
        "type": "option",
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 1,
        "right": "C",
        "expiry": "2026-12-18",
        "strike": 500.0,
        "client_attempt_id": attempt,
    }
    candidate, legs = gate4.candidate_from_place_body(body, SCOPE["broker_account"])
    return gate4.reserve_with_coverage(
        "u1",
        attempt,
        req or _sell_call_req(),
        candidate=candidate,
        legs_payload=legs,
        scope=SCOPE,
    )


def _sub_row(attempt):
    engine = get_sync_engine()
    with engine.begin() as conn:
        return conn.execute(
            select(order_submissions).where(order_submissions.c.client_attempt_id == attempt)
        ).first()


# ---------------------------------------------------------------------------
# accept / reject basics
# ---------------------------------------------------------------------------


def test_covered_sell_call_wins_and_persists_legs():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 100)]))
    out = _reserve("att-covered-1")
    assert out.status == "winner"
    row = _sub_row("att-covered-1")
    assert row is not None
    assert row.legs["guaranteed"] is False
    assert row.legs["legs"][0]["sec"] == "OPT"
    assert row.legs["legs"][0]["und_sec"] == "STK"


def test_naked_sell_call_rejected_and_no_row():
    _seed_snapshot(_snapshot_payload([]))
    out = _reserve("att-naked-1")
    assert out.status == "rejected"
    assert any("uncovered_call_increase" in r for r in out.reasons)
    assert _sub_row("att-naked-1") is None


def test_short_stock_rejected():
    _seed_snapshot(_snapshot_payload([]))
    body = {
        "type": "stock",
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 100,
        "client_attempt_id": "att-shortstk-1",
    }
    candidate, legs = gate4.candidate_from_place_body(body, SCOPE["broker_account"])
    out = gate4.reserve_with_coverage(
        "u1",
        "att-shortstk-1",
        RequestRow(ticker="SPY", security_type="STK", action="SELL", quantity=100, multiplier=100, limit_price=Decimal("500")),
        candidate=candidate,
        legs_payload=legs,
        scope=SCOPE,
    )
    assert out.status == "rejected"
    assert any("short_stock" in r for r in out.reasons)


def test_buy_single_leg_passes_without_snapshot():
    # Pinned pass-through: a single-leg BUY creates no demand and must not be
    # gated on view completeness — there is no snapshot row at all here.
    body = {
        "type": "stock",
        "symbol": "SPY",
        "action": "BUY",
        "quantity": 100,
        "client_attempt_id": "att-buy-1",
    }
    candidate, legs = gate4.candidate_from_place_body(body, SCOPE["broker_account"])
    assert candidate is None
    out = gate4.reserve_with_coverage(
        "u1",
        "att-buy-1",
        RequestRow(ticker="SPY", security_type="STK", action="BUY", quantity=100, multiplier=100, limit_price=Decimal("500")),
        candidate=candidate,
        legs_payload=legs,
        scope=SCOPE,
    )
    assert out.status == "winner"


def test_sell_without_snapshot_fails_closed():
    out = _reserve("att-nosnap-1")
    assert out.status == "rejected"
    assert "view_incomplete" in out.reasons


def test_incomplete_observation_fails_closed():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 100)], complete=False))
    out = _reserve("att-incomplete-1")
    assert out.status == "rejected"
    assert "view_incomplete" in out.reasons


def test_snapshot_missing_observation_fails_closed():
    # Pre-PR-03b snapshot: pass never attested read completeness.
    _seed_snapshot({"positions": [_stock("SPY", 100)]})
    out = _reserve("att-noobs-1")
    assert out.status == "rejected"
    assert "view_incomplete" in out.reasons


def test_stale_snapshot_fails_closed(monkeypatch):
    monkeypatch.setenv("XENON_PORTFOLIO_SNAPSHOT_STALE_CLOSED_S", "10")
    old = NOW - timedelta(hours=2)
    _seed_snapshot(_snapshot_payload([_stock("SPY", 100)], as_of=old), snapshot_at=old)
    out = _reserve("att-stale-1")
    assert out.status == "rejected"
    assert "view_stale" in out.reasons


# ---------------------------------------------------------------------------
# fills / orders as demand
# ---------------------------------------------------------------------------


def _insert_submission(attempt, *, state="WORKING", qty=1, action="SELL", sec="OPT", legs=None, scope=None):
    engine = get_sync_engine()
    sc = scope or SCOPE
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=f"sub-{attempt}",
                user_id="u1",
                client_attempt_id=attempt,
                ticker="SPY",
                security_type=sec,
                action=action,
                quantity=qty,
                expiry=datetime(2026, 12, 18).date() if sec != "STK" else None,
                strike=Decimal("500") if sec == "OPT" else None,
                right="C" if sec == "OPT" else None,
                multiplier=100,
                con_id=111 if sec == "OPT" else None,
                limit_price=Decimal("5"),
                state=state,
                submitted_at=NOW,
                updated_at=NOW,
                legs=legs,
                ib_order_id=str(hash(attempt) % 100000),
                **sc,
            )
        )
    return f"sub-{attempt}"


def _insert_fill(exec_id, *, sub_id=None, perm_id=None, qty=1, filled_at=None, sec_type="OPT", scope=None, legacy=False):
    engine = get_sync_engine()
    sc = scope or SCOPE
    meta = {"sec_type": sec_type} if sec_type else {}
    if legacy:
        meta["legacy_source"] = "ib_mirror"
    with engine.begin() as conn:
        conn.execute(
            insert(order_fills).values(
                exec_id=exec_id,
                submission_id=sub_id,
                perm_id=str(perm_id) if perm_id else None,
                con_id=111,
                ticker="SPY",
                side="SLD",
                qty=Decimal(str(qty)),
                price=Decimal("5"),
                filled_at=filled_at or NOW,
                metadata=meta,
                **sc,
            )
        )


def test_fill_after_snapshot_consumes_cover():
    """adv-2: a fill landing after the holdings as_of still consumes cover —
    the second short call on the same 100-share block must be blocked."""
    _seed_snapshot(_snapshot_payload([_stock("SPY", 100)], as_of=NOW - timedelta(minutes=5)))
    sub_id = _insert_submission("att-o1", state="FILLED", qty=1)
    _insert_fill("exec-1", sub_id=sub_id, qty=1, filled_at=NOW)
    out = _reserve("att-second-1")
    assert out.status == "rejected"
    assert any("uncovered_call_increase" in r for r in out.reasons)


def test_fill_before_snapshot_does_not_double_count():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 100)], as_of=NOW))
    sub_id = _insert_submission("att-o1b", state="FILLED", qty=1)
    # Fill is INSIDE the as_of window — the snapshot's 100 shares already
    # reflect the post-fill account, so this is not new demand.
    _insert_fill("exec-1b", sub_id=sub_id, qty=1, filled_at=NOW - timedelta(minutes=5))
    out = _reserve("att-next-1")
    assert out.status == "winner"


def test_observation_stamp_completeness():
    """ib_sync._observation_stamp: `complete` requires all three reads plus a
    known account — anything less and the safety view is unattested."""
    from xenon.execution import ib_sync

    t0 = NOW - timedelta(seconds=30)
    full = {"positions": True, "open_orders": True, "executions": True}
    stamp = ib_sync._observation_stamp(t0, full, "DU0000000")
    assert stamp["complete"] is True
    assert stamp["as_of"] == t0.isoformat()
    assert stamp["account"] == "DU0000000"
    assert stamp["reads"] == full

    # A single failed read anywhere in the pass degrades the whole stamp.
    for key in ("positions", "open_orders", "executions"):
        partial = dict(full, **{key: False})
        assert ib_sync._observation_stamp(t0, partial, "DU0000000")["complete"] is False

    # Account unknown → unattested even when every read succeeded.
    assert ib_sync._observation_stamp(t0, full, "")["complete"] is False
    assert ib_sync._observation_stamp(t0, full, None)["complete"] is False


@pytest.mark.parametrize("inject_after", ["positions", "open_orders", "executions"])
def test_fill_injected_between_sync_reads_blocks_next_short(inject_after):
    """A fill landing between ANY pair of sync reads still post-dates the
    observation as_of (pass START), so gate4 counts it as demand the
    holdings snapshot cannot show → the next short is blocked.

    Read order is positions → executions → open_orders; injecting after each
    pair boundary exercises every mid-pass window.
    """
    t0 = NOW - timedelta(minutes=5)          # pass start → observation.as_of
    t_save = NOW - timedelta(seconds=10)     # snapshot persisted after reads
    t_fill = t0 + (t_save - t0) / 2          # injected mid-pass, any window
    _seed_snapshot(
        _snapshot_payload([_stock("SPY", 100)], as_of=t0),
        snapshot_at=t_save,
    )
    sub_id = _insert_submission(f"att-inj-{inject_after}", state="FILLED", qty=1)
    _insert_fill(f"exec-inj-{inject_after}", sub_id=sub_id, qty=1, filled_at=t_fill)
    out = _reserve(f"att-after-inj-{inject_after}")
    assert out.status == "rejected"
    assert any("uncovered_call_increase" in r for r in out.reasons)


def test_cancelled_order_post_snapshot_fill_counts():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 100)], as_of=NOW - timedelta(minutes=5)))
    sub_id = _insert_submission("att-cx", state="CANCELLED", qty=2)
    _insert_fill("exec-cx", sub_id=sub_id, qty=1, filled_at=NOW)
    out = _reserve("att-after-cx")
    assert out.status == "rejected"


def test_working_order_reserves_cover():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 100)]))
    _insert_submission("att-w1", state="WORKING", qty=1)
    out = _reserve("att-w2")
    assert out.status == "rejected"


def test_unresolved_bag_row_fails_closed():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 100)]))
    _insert_submission("att-bag-null", state="WORKING", qty=1, sec="BAG", legs=None)
    out = _reserve("att-next-bag")
    assert out.status == "rejected"
    assert any("order_unresolved_legs" in r for r in out.reasons)


def test_orphan_fill_after_as_of_marks_view_incomplete():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 100)], as_of=NOW - timedelta(minutes=5)))
    # An orphan fill (perm_id not seen in our submissions) post-dating the
    # observation boundary is demand we cannot model — view must fail closed.
    # ck_fills_source_present requires a provenance marker on unattributed fills.
    _insert_fill("exec-orphan", sub_id=None, perm_id=999999, qty=1, filled_at=NOW, legacy=True)
    out = _reserve("att-orphan-1")
    assert out.status == "rejected"
    assert "view_incomplete" in out.reasons


def test_perm_id_attribution_resolves_fills():
    """Fills mirrored without submission_id still attribute via perm_id."""
    _seed_snapshot(_snapshot_payload([_stock("SPY", 100)], as_of=NOW - timedelta(minutes=5)))
    sub_id = _insert_submission("att-perm", state="FILLED", qty=1)
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE xenon.order_submissions SET perm_id='424242' WHERE submission_id=:s"),
            {"s": sub_id},
        )
    _insert_fill("exec-perm", sub_id=None, perm_id=424242, qty=1, filled_at=NOW, legacy=True)
    out = _reserve("att-perm-next")
    assert out.status == "rejected"


def test_foreign_scope_rows_do_not_consume():
    """Orders/fills stamped to a foreign account never consume our cover."""
    foreign = {"broker": "IB", "account_env": "live", "broker_account": "U_FOREIGN"}
    _seed_snapshot(_snapshot_payload([_stock("SPY", 100)]))
    sub_id = _insert_submission("att-foreign", state="WORKING", qty=1, scope=foreign)
    _insert_fill("exec-foreign", sub_id=sub_id, qty=1, filled_at=NOW, scope=foreign)
    out = _reserve("att-own-1")
    assert out.status == "winner"


def test_sell_envelope_combo_flips_legs():
    """A SELL-envelope BAG is NOT exempt: legs flip and demand is evaluated."""
    _seed_snapshot(_snapshot_payload([]))
    body = {
        "type": "combo",
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 1,
        "client_attempt_id": "att-sellenv-1",
        "legs": [
            {"action": "BUY", "right": "C", "expiry": "2026-12-18", "strike": 500.0, "ratio": 1},
            {"action": "SELL", "right": "C", "expiry": "2026-12-18", "strike": 510.0, "ratio": 1},
        ],
    }
    candidate, legs = gate4.candidate_from_place_body(body, SCOPE["broker_account"])
    out = gate4.reserve_with_coverage(
        "u1",
        "att-sellenv-1",
        RequestRow(ticker="SPY", security_type="BAG", action="SELL", quantity=1, multiplier=100, limit_price=Decimal("1")),
        candidate=candidate,
        legs_payload=legs,
        scope=SCOPE,
    )
    # SELL env flips -> effective SELL 500C + BUY 510C: net short call at 500.
    assert out.status == "rejected"
    # With 100 shares the flipped short call is covered -> winner.
    _seed_snapshot(
        _snapshot_payload([_stock("SPY", 100)], as_of=NOW + timedelta(seconds=5)),
        snapshot_at=NOW + timedelta(seconds=5),
    )
    body["client_attempt_id"] = "att-sellenv-2"
    candidate, legs = gate4.candidate_from_place_body(body, SCOPE["broker_account"])
    out2 = gate4.reserve_with_coverage(
        "u1",
        "att-sellenv-2",
        RequestRow(ticker="SPY", security_type="BAG", action="SELL", quantity=1, multiplier=100, limit_price=Decimal("1")),
        candidate=candidate,
        legs_payload=legs,
        scope=SCOPE,
    )
    assert out2.status == "winner"


def test_guaranteed_bag_mode_persisted_and_netting(monkeypatch):
    """nonGuaranteed=0 persists guaranteed=True; netting evaluates the BAG
    per-key when XENON_GATE4_NETTING is on, leg-by-leg when off."""
    monkeypatch.setenv("XENON_GATE4_NETTING", "1")
    _seed_snapshot(_snapshot_payload([_opt_leg_pos("SPY", 1, strike=500), _opt_leg_pos("SPY", -1, strike=510)]))
    body = {
        "type": "combo",
        "symbol": "SPY",
        "action": "SELL",
        "quantity": 1,
        "nonGuaranteed": 0,
        "client_attempt_id": "att-gbag-1",
        "legs": [
            {"action": "BUY", "right": "C", "expiry": "2026-12-18", "strike": 500.0, "ratio": 1},
            {"action": "SELL", "right": "C", "expiry": "2026-12-18", "strike": 510.0, "ratio": 1},
        ],
    }
    candidate, legs = gate4.candidate_from_place_body(body, SCOPE["broker_account"])
    assert legs["guaranteed"] is True
    out = gate4.reserve_with_coverage(
        "u1",
        "att-gbag-1",
        RequestRow(ticker="SPY", security_type="BAG", action="SELL", quantity=1, multiplier=100, limit_price=Decimal("1")),
        candidate=candidate,
        legs_payload=legs,
        scope=SCOPE,
    )
    assert out.status == "winner"
    row = _sub_row("att-gbag-1")
    assert row.legs["guaranteed"] is True


# ---------------------------------------------------------------------------
# Idempotent replay under the same lock
# ---------------------------------------------------------------------------


def test_duplicate_replay_skips_coverage():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 100)]))
    first = _reserve("att-dup-1")
    assert first.status == "winner"
    # Cover is now consumed — a *different* attempt would reject, but the
    # same-attempt replay must return the existing row untouched.
    replay = _reserve("att-dup-1")
    assert replay.status == "duplicate"
    assert replay.submission_id == first.submission_id


def test_body_mismatch_same_attempt():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 100)]))
    _reserve("att-mm-1")
    out = _reserve("att-mm-1", req=_sell_call_req(qty=2))
    assert out.status == "body_mismatch"


# ---------------------------------------------------------------------------
# Modify reservations
# ---------------------------------------------------------------------------


def _working_with_legs(attempt, qty=1):
    legs = gate4.legs_payload_for(
        legs=[
            coverage.Leg(
                action="BUY", sym="SPY", sec="OPT", ratio=1, right="C",
                exp="20261218", mult=100, tc="SPY", strike=500.0, con_id=111, und_sec="STK",
            )
        ],
        guaranteed=False,
    )
    return _insert_submission(attempt, state="WORKING", qty=qty, legs=legs)


def test_begin_modify_reserves_max_until_settled():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 250)]))
    sub_id = _working_with_legs("att-mod-1", qty=1)
    out = gate4.begin_modify(sub_id, 2, SCOPE)
    assert out.accept
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(
            select(order_submissions.c.legs).where(order_submissions.c.submission_id == sub_id)
        ).first()
    assert row[0]["pending_amend_qty"] == 2
    # While the amend is pending, reserved demand = max(1,2) - 0 = 2 calls;
    # 250 shares cover exactly 2 -> the next short call is uncovered -> reject.
    nxt = _reserve("att-after-mod")
    assert nxt.status == "rejected"


def test_begin_modify_rejects_when_cover_insufficient():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 150)]))
    sub_id = _working_with_legs("att-mod-2", qty=1)
    out = gate4.begin_modify(sub_id, 2, SCOPE)
    assert not out.accept
    assert any("uncovered_call_increase" in r for r in out.reasons)


def test_settle_confirmed_applies_new_qty():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 250)]))
    sub_id = _working_with_legs("att-mod-3", qty=1)
    assert gate4.begin_modify(sub_id, 2, SCOPE).accept
    gate4.settle_modify(sub_id, "confirmed", new_qty=2)
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(
            select(order_submissions.c.quantity, order_submissions.c.legs).where(
                order_submissions.c.submission_id == sub_id
            )
        ).first()
    assert row[0] == 2
    assert row[1]["pending_amend_qty"] is None


def test_settle_uncertain_keeps_max_reservation():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 250)]))
    sub_id = _working_with_legs("att-mod-4", qty=1)
    assert gate4.begin_modify(sub_id, 2, SCOPE).accept
    gate4.settle_modify(sub_id, "uncertain")
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(
            select(order_submissions.c.legs).where(order_submissions.c.submission_id == sub_id)
        ).first()
    assert row[0]["pending_amend_qty"] == 2
    # Reservation still counts max(1,2)-filled=2 -> 250 covers 2 -> reject.
    assert _reserve("att-unc-next").status == "rejected"


def test_settle_rejected_clears_pending():
    _seed_snapshot(_snapshot_payload([_stock("SPY", 300)]))
    sub_id = _working_with_legs("att-mod-5", qty=1)
    assert gate4.begin_modify(sub_id, 2, SCOPE).accept
    gate4.settle_modify(sub_id, "rejected")
    engine = get_sync_engine()
    with engine.begin() as conn:
        row = conn.execute(
            select(order_submissions.c.legs).where(order_submissions.c.submission_id == sub_id)
        ).first()
    assert row[0]["pending_amend_qty"] is None


# ---------------------------------------------------------------------------
# Cross-process serialization + restart rebuild (committed DB)
# ---------------------------------------------------------------------------


def _env_for_subprocess():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    return env


_CHILD = r"""
import sys
from decimal import Decimal
from xenon.execution import gate4
from xenon.execution.orders_store import RequestRow

SCOPE = {"broker": "IB", "account_env": "paper", "broker_account": "DU0000000"}
attempt, mode = sys.argv[1], sys.argv[2]
if mode == "sell":
    body = {"type": "option", "symbol": "SPY", "action": "SELL", "quantity": 1,
            "right": "C", "expiry": "2026-12-18", "strike": 500.0,
            "client_attempt_id": attempt}
    cand, legs = gate4.candidate_from_place_body(body, SCOPE["broker_account"])
    out = gate4.reserve_with_coverage(
        "u1", attempt,
        RequestRow(ticker="SPY", security_type="OPT", action="SELL", quantity=1,
                   expiry="2026-12-18", strike=Decimal("500"), right="C",
                   multiplier=100, con_id=111, limit_price=Decimal("5")),
        candidate=cand, legs_payload=legs, scope=SCOPE)
    print(out.status)
"""


@pytest.mark.committed_db
def test_two_processes_race_for_one_cover():
    """Two separate processes racing for the last unit of cover: exactly one
    must be accepted. The advisory lock + atomic check+insert is what makes
    this safe — on the pre-PR-03b path (separate check and insert txns) both
    could observe the same free cover and both reserve."""
    as_of = NOW - timedelta(minutes=1)
    payload = _snapshot_payload([_stock("SPY", 100)], as_of=as_of)
    from sqlalchemy import create_engine

    eng = create_engine(os.environ["DATABASE_URL"])
    with eng.begin() as conn:
        conn.execute(
            insert(account_snapshots).values(
                account="DU0000000",
                bankroll=Decimal("0"),
                payload=payload,
                snapshot_at=NOW,
                **SCOPE,
            )
        )
    eng.dispose()

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, f"race-{i}", "sell"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_env_for_subprocess(),
            text=True,
        )
        for i in range(4)
    ]
    statuses = [p.communicate(timeout=60)[0].strip() for p in procs]
    assert statuses.count("winner") == 1, f"expected exactly one winner, got {statuses}"
    assert all(s == "rejected" for s in statuses if s != "winner")


@pytest.mark.committed_db
def test_restart_rebuilds_reservations_from_postgres():
    """A second process with zero in-memory state must see the first
    process's durable reservation — cover consumed before 'restart' still
    blocks the next short."""
    payload = _snapshot_payload([_stock("SPY", 100)], as_of=NOW - timedelta(minutes=1))
    from sqlalchemy import create_engine

    eng = create_engine(os.environ["DATABASE_URL"])
    with eng.begin() as conn:
        conn.execute(
            insert(account_snapshots).values(
                account="DU0000000",
                bankroll=Decimal("0"),
                payload=payload,
                snapshot_at=NOW,
                **SCOPE,
            )
        )
    eng.dispose()

    p1 = subprocess.run(
        [sys.executable, "-c", _CHILD, "restart-a", "sell"],
        capture_output=True, env=_env_for_subprocess(), text=True, timeout=60,
    )
    assert p1.stdout.strip() == "winner", p1.stderr
    p2 = subprocess.run(
        [sys.executable, "-c", _CHILD, "restart-b", "sell"],
        capture_output=True, env=_env_for_subprocess(), text=True, timeout=60,
    )
    assert p2.stdout.strip() == "rejected", p2.stderr + p2.stdout
