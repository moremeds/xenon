"""Tests for combo_wizard.rehydrate — restart-safe reconcile for wizard sessions.

Covers the critical BAG per-leg aggregation rule from spec §13 and plan lines
416-422: IB reports combo fills as per-leg execution rows sharing one parent
`permId`. The rehydrate MUST group executions by permId, sum each leg's
`shares` against the expected ratio, and only mark FILLED when every leg
reached ratio * totalQuantity.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from xenon.execution import orders_store
from xenon.execution.combo_wizard import rehydrate as wiz_rehydrate


def _seed_session(
    db_path: Path,
    *,
    state: str = "working",
    perm_id: str = "P-1",
    ib_order_id: str = "IB-1",
    quantity: int = 1,
    legs: list[dict] | None = None,
) -> tuple[str, str]:
    orders_store.init_store(db_path)
    sid = f"wiz-{uuid.uuid4().hex[:12]}"
    aid = uuid.uuid4().hex
    legs = legs or [
        {"conId": 1001, "action": "BUY", "ratio": 1},
        {"conId": 1002, "action": "SELL", "ratio": 1},
    ]
    payload = {
        "symbol": "AAPL",
        "type": "combo",
        "action": "BUY",
        "quantity": quantity,
        "legs": legs,
    }
    now = datetime.now(timezone.utc)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            INSERT INTO wizard_sessions (session_id, ticker, state, structure_name,
                intent, payload_json, current_attempt_id, created_at, updated_at)
            VALUES (?, 'AAPL', ?, 'Bull Call Spread', 'OPEN', ?, ?, ?, ?)
            """,
            [sid, state, json.dumps(payload), aid, now, now],
        )
        con.execute(
            """
            INSERT INTO wizard_combo_attempts (attempt_id, session_id, client_attempt_id,
                ib_order_id, perm_id, intent, target_price, price_basis, submitted_at,
                terminal_state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'OPEN', '2.50', 'MID', ?, 'WORKING', ?, ?)
            """,
            [aid, sid, f"wiz:{sid}:combo:{aid}", ib_order_id, perm_id, now, now, now],
        )
    finally:
        con.close()
    return sid, aid


class _StubIB:
    def __init__(self, *, open_orders=None, executions=None, positions=None):
        self._open = open_orders or []
        self._execs = executions or []
        self._positions = positions or []

    def get_open_orders(self):
        return self._open

    def get_executions(self):
        return self._execs

    def get_positions(self):
        return self._positions


# ---------------------------------------------------------------------------
# BAG per-leg aggregation — the critical regression rule
# ---------------------------------------------------------------------------


def test_combo_rehydrate_partial_leg_fills_stays_partially_filled(tmp_path, monkeypatch):
    """Two leg executions (one partial, one full) must NOT mark the attempt
    FILLED — the session must remain PARTIALLY_FILLED."""
    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    sid, aid = _seed_session(db, state="working", perm_id="P-1", quantity=1)

    # Order qty=1, ratio=1 per leg → each leg needs shares=1 to be FILLED.
    # Leg 1001: shares=1 (full). Leg 1002: shares=0 (none yet).
    # permId is shared across legs for a BAG.
    execs = [
        {"perm_id": "P-1", "con_id": 1001, "shares": 1, "avg_price": 3.10},
        # No execution row at all for leg 1002.
    ]
    ib = _StubIB(open_orders=[], executions=execs)

    decisions = wiz_rehydrate.rehydrate_combo_sessions(ib_client_factory=lambda: ib, db_path=db)

    assert len(decisions) == 1
    assert decisions[0].to_state == "PARTIALLY_FILLED"

    con = duckdb.connect(str(db))
    try:
        state = con.execute("SELECT state FROM wizard_sessions WHERE session_id=?", [sid]).fetchone()[0]
    finally:
        con.close()
    assert state.upper() == "PARTIALLY_FILLED"


def test_combo_rehydrate_all_legs_full_ratio_marks_filled(tmp_path, monkeypatch):
    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    sid, _ = _seed_session(db, state="working", perm_id="P-2", quantity=2)

    # qty=2, ratio=1 each → each leg needs shares=2 to be FILLED.
    execs = [
        {"perm_id": "P-2", "con_id": 1001, "shares": 2, "avg_price": 3.10},
        {"perm_id": "P-2", "con_id": 1002, "shares": 2, "avg_price": 1.30},
    ]
    ib = _StubIB(executions=execs)

    decisions = wiz_rehydrate.rehydrate_combo_sessions(ib_client_factory=lambda: ib, db_path=db)

    assert decisions[0].to_state == "FILLED"

    con = duckdb.connect(str(db))
    try:
        state = con.execute("SELECT state FROM wizard_sessions WHERE session_id=?", [sid]).fetchone()[0]
    finally:
        con.close()
    assert state.upper() == "FILLED"


def test_combo_rehydrate_one_leg_missing_stays_partially_filled(tmp_path, monkeypatch):
    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    _seed_session(db, state="working", perm_id="P-3", quantity=1)

    execs = [
        {"perm_id": "P-3", "con_id": 1001, "shares": 1, "avg_price": 3.10},
        # Leg 1002 fully absent.
    ]
    ib = _StubIB(executions=execs)
    decisions = wiz_rehydrate.rehydrate_combo_sessions(ib_client_factory=lambda: ib, db_path=db)
    assert decisions[0].to_state == "PARTIALLY_FILLED"


def test_combo_rehydrate_overfill_on_one_leg_still_partially_filled(tmp_path, monkeypatch):
    """If one leg overfills (e.g. a ratio miscount at IB) but the other leg
    hasn't reached its target, we stay PARTIALLY_FILLED — we never claim
    FILLED on a mismatched combo."""
    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    _seed_session(db, state="working", perm_id="P-4", quantity=1)

    execs = [
        {"perm_id": "P-4", "con_id": 1001, "shares": 3, "avg_price": 3.10},  # overfill
        {"perm_id": "P-4", "con_id": 1002, "shares": 0, "avg_price": 0.0},  # nothing
    ]
    ib = _StubIB(executions=execs)
    decisions = wiz_rehydrate.rehydrate_combo_sessions(ib_client_factory=lambda: ib, db_path=db)
    assert decisions[0].to_state == "PARTIALLY_FILLED"


def test_combo_rehydrate_ratio_2_for_one_leg(tmp_path, monkeypatch):
    """Ratio 1:2 (e.g. a ratio spread) — FILLED only when leg 2 has 2x shares."""
    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    legs = [
        {"conId": 1001, "action": "BUY", "ratio": 1},
        {"conId": 1002, "action": "SELL", "ratio": 2},
    ]
    _seed_session(db, state="working", perm_id="P-5", quantity=1, legs=legs)

    # qty=1, ratio 1 & 2 → need shares 1 and 2 respectively.
    # Here leg2 only has 1 share → PARTIALLY_FILLED.
    execs = [
        {"perm_id": "P-5", "con_id": 1001, "shares": 1, "avg_price": 3.10},
        {"perm_id": "P-5", "con_id": 1002, "shares": 1, "avg_price": 1.30},
    ]
    ib = _StubIB(executions=execs)
    decisions = wiz_rehydrate.rehydrate_combo_sessions(ib_client_factory=lambda: ib, db_path=db)
    assert decisions[0].to_state == "PARTIALLY_FILLED"


# ---------------------------------------------------------------------------
# Open-orders path: still WORKING
# ---------------------------------------------------------------------------


def test_combo_rehydrate_open_order_stays_working(tmp_path, monkeypatch):
    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    _seed_session(db, state="working", perm_id="P-6")

    ib = _StubIB(open_orders=[{"perm_id": "P-6", "status": "Submitted"}], executions=[])

    decisions = wiz_rehydrate.rehydrate_combo_sessions(ib_client_factory=lambda: ib, db_path=db)
    assert decisions[0].to_state == "WORKING"


# ---------------------------------------------------------------------------
# Only rehydratable states are picked up
# ---------------------------------------------------------------------------


def test_combo_rehydrate_skips_terminal_states(tmp_path, monkeypatch):
    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    _seed_session(db, state="aborted", perm_id="P-7")
    _seed_session(db, state="rejected", perm_id="P-8")
    _seed_session(db, state="filled", perm_id="P-9")  # FILLED not in rehydrate set

    ib = _StubIB()
    decisions = wiz_rehydrate.rehydrate_combo_sessions(ib_client_factory=lambda: ib, db_path=db)
    assert decisions == []


def test_combo_rehydrate_picks_up_protection_pending_session(tmp_path, monkeypatch):
    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    sid, _ = _seed_session(db, state="PROTECTION_PENDING", perm_id="P-10")

    ib = _StubIB()
    decisions = wiz_rehydrate.rehydrate_combo_sessions(ib_client_factory=lambda: ib, db_path=db)
    # PROTECTION_PENDING sessions surface an "awaiting protection retry" decision.
    assert len(decisions) == 1
    assert decisions[0].detail.get("reason_code") in {
        "PROTECTION_RETRY_REQUIRED",
        "PROTECTION_REDRIVE",
    }
