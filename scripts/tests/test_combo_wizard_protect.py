"""Tests for combo_wizard.protect — post-fill protection pipeline.

Covers:
- Successful TP + Risk Alert attach transitions PROTECTION_PENDING → PROTECTED
- Retry with backoff on transient failures
- Terminal failure leaves session in PROTECTION_PENDING (not PROTECTED)
- Naked-short guard: TP that would short an uncovered leg is refused, Risk Alert still armed
- "Risk Alert" copy never says "stop-loss"
- Idempotent: re-running on an already-PROTECTED session is a no-op
- Signed combo pricing preserved (no abs() applied)
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from xenon.execution import orders_store
from xenon.execution.combo_wizard import protect
from xenon.execution.combo_wizard import session as wiz_session
from xenon.execution.combo_wizard import store as wiz_store


def _init_session(db_path: Path, *, state: str = "FILLED", payload: dict | None = None) -> str:
    orders_store.init_store(db_path)
    # Seed a session directly so we don't need the full wiz_session.create_session
    # fixture path (which requires XENON_API_TEST_MODE etc).
    import uuid
    from datetime import datetime, timezone

    session_id = f"wiz-{uuid.uuid4().hex[:12]}"
    payload = payload or {
        "symbol": "AAPL",
        "type": "combo",
        "action": "BUY",
        "quantity": 1,
        "limitPrice": "2.50",
        "legs": [
            {"conId": 1001, "action": "BUY", "ratio": 1},
            {"conId": 1002, "action": "SELL", "ratio": 1},
        ],
    }
    now = datetime.now(timezone.utc)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            INSERT INTO wizard_sessions (session_id, ticker, state, structure_name,
                intent, payload_json, created_at, updated_at)
            VALUES (?, 'AAPL', ?, 'Bull Call Spread', 'OPEN', ?, ?, ?)
            """,
            [session_id, state, json.dumps(payload), now, now],
        )
    finally:
        con.close()
    return session_id


class _StubIB:
    """Injectable stub for the IB side of protect. `tp_attach` and `arm_alert`
    return a dict-shaped ack or raise.
    """

    def __init__(self, *, tp_acks: list, alert_acks: list):
        # Each ack is either a dict (success) or an Exception (failure).
        self._tp_acks = list(tp_acks)
        self._alert_acks = list(alert_acks)
        self.tp_calls: list = []
        self.alert_calls: list = []

    def place_combo_tp(self, *, session_id, legs, target_price, quantity):
        self.tp_calls.append({"session_id": session_id, "target_price": target_price})
        ack = self._tp_acks.pop(0) if self._tp_acks else Exception("no more tp acks")
        if isinstance(ack, Exception):
            raise ack
        return ack

    def register_risk_alert(self, *, session_id, threshold, polarity):
        self.alert_calls.append({"session_id": session_id, "threshold": threshold, "polarity": polarity})
        ack = self._alert_acks.pop(0) if self._alert_acks else Exception("no more alert acks")
        if isinstance(ack, Exception):
            raise ack
        return ack


def test_protection_success_transitions_protected(tmp_path, monkeypatch):
    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    sid = _init_session(db, state="FILLED")

    ib = _StubIB(
        tp_acks=[{"order_id": 9001, "perm_id": "p-9001"}],
        alert_acks=[{"virtual_id": "alert-1"}],
    )

    result = protect.attach_protection(
        sid,
        ib=ib,
        tp_target_price=Decimal("3.50"),  # DEBIT close target for a BUY debit spread
        alert_net_mid_threshold=Decimal("1.25"),
        polarity="DEBIT",
        sleep=lambda _s: None,
    )

    assert result["state"] == "PROTECTED"
    assert result["tp_attached"] is True
    assert result["alert_armed"] is True
    assert result["attempts"] == 1

    con = duckdb.connect(str(db))
    try:
        state = con.execute("SELECT state FROM wizard_sessions WHERE session_id=?", [sid]).fetchone()[0]
        prot = con.execute(
            "SELECT tp_enabled, tp_target_price, alert_enabled, alert_net_mid_threshold "
            "FROM wizard_protection WHERE session_id=?",
            [sid],
        ).fetchone()
    finally:
        con.close()
    assert state == "PROTECTED"
    assert prot is not None
    assert bool(prot[0]) is True
    assert Decimal(str(prot[1])) == Decimal("3.50")
    assert bool(prot[2]) is True


def test_protection_pending_retries_then_fails_if_tp_attach_never_acks(tmp_path, monkeypatch):
    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    sid = _init_session(db, state="FILLED")

    ib = _StubIB(
        tp_acks=[RuntimeError("timeout"), RuntimeError("timeout"), RuntimeError("timeout")],
        alert_acks=[{"virtual_id": "alert-1"}],  # not reached
    )
    sleeps: list[float] = []

    result = protect.attach_protection(
        sid,
        ib=ib,
        tp_target_price=Decimal("3.50"),
        alert_net_mid_threshold=Decimal("1.25"),
        polarity="DEBIT",
        sleep=sleeps.append,
        max_attempts=3,
        base_backoff=2.0,
    )

    assert result["state"] == "PROTECTION_PENDING"
    assert result["tp_attached"] is False
    assert result["attempts"] == 3
    # Exponential backoff: sleeps between attempts 1→2 and 2→3
    assert sleeps == [2.0, 4.0]

    con = duckdb.connect(str(db))
    try:
        state = con.execute("SELECT state FROM wizard_sessions WHERE session_id=?", [sid]).fetchone()[0]
        events = con.execute("SELECT kind FROM wizard_session_events WHERE session_id=?", [sid]).fetchall()
    finally:
        con.close()
    assert state == "PROTECTION_PENDING"
    # A session event must be emitted on protection failure.
    kinds = [row[0] for row in events]
    assert any("PROTECTION" in k for k in kinds)


def test_protection_idempotent_on_already_protected(tmp_path, monkeypatch):
    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    sid = _init_session(db, state="PROTECTED")

    ib = _StubIB(tp_acks=[], alert_acks=[])

    result = protect.attach_protection(
        sid,
        ib=ib,
        tp_target_price=Decimal("3.50"),
        alert_net_mid_threshold=Decimal("1.25"),
        polarity="DEBIT",
        sleep=lambda _s: None,
    )

    assert result["state"] == "PROTECTED"
    assert result.get("noop") is True
    # No IB calls were made.
    assert ib.tp_calls == []
    assert ib.alert_calls == []


def test_naked_short_guard_refuses_tp_but_arms_alert(tmp_path, monkeypatch):
    """If the TP would short an uncovered call leg, we skip the TP and route
    to Risk Alert only. This keeps Gate-4 intact."""
    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    # Payload describes a short risk reversal: SELL C + BUY P (naked short call).
    # This is a structure we would NOT have accepted at entry, but we still
    # guard at protect-time to be defensive.
    payload = {
        "symbol": "AAPL",
        "type": "combo",
        "action": "BUY",
        "quantity": 1,
        "limitPrice": "-0.20",
        "legs": [
            {"conId": 2001, "action": "SELL", "ratio": 1, "right": "C", "strike": "200"},
            {"conId": 2002, "action": "BUY", "ratio": 1, "right": "P", "strike": "180"},
        ],
    }
    sid = _init_session(db, state="FILLED", payload=payload)

    ib = _StubIB(
        tp_acks=[{"order_id": 9001, "perm_id": "p"}],
        alert_acks=[{"virtual_id": "alert-1"}],
    )

    result = protect.attach_protection(
        sid,
        ib=ib,
        tp_target_price=Decimal("-0.50"),
        alert_net_mid_threshold=Decimal("-0.80"),
        polarity="CREDIT",
        sleep=lambda _s: None,
    )

    assert result["tp_attached"] is False
    assert result["tp_refused_reason"] == "NAKED_SHORT_GUARD"
    assert result["alert_armed"] is True
    # TP must not have been sent to IB.
    assert ib.tp_calls == []


def test_risk_alert_copy_never_says_stop_loss():
    """Spec §9.2 — the popup must say Risk Alert → Assisted Exit, NOT stop-loss."""
    text = protect.risk_alert_popup_copy()
    low = text.lower()
    assert "risk alert" in low
    assert "assisted exit" in low
    assert "stop-loss" not in low
    assert "stop loss" not in low


def test_naked_short_guard_error_short_circuits_retry_loop(tmp_path, monkeypatch):
    """If the adapter raises NakedShortGuardError (e.g., IB-201 terminal
    broker reject), the retry loop must NOT re-invoke the adapter 3x —
    retrying a terminal Gate-4 refusal wastes 14+ seconds. The session
    should land in the terminal refused state on the first attempt and
    the Risk Alert path should still arm."""
    from xenon.execution.combo_wizard.ib_adapter import NakedShortGuardError

    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    sid = _init_session(db, state="FILLED")

    calls: list[dict] = []

    class _RaisingIB:
        def place_combo_tp(self, **kwargs):
            calls.append(kwargs)
            raise NakedShortGuardError("IB error 201: terminal reject")

        def register_risk_alert(self, **kwargs):
            return {"virtual_id": "alert-1"}

    sleeps: list[float] = []
    result = protect.attach_protection(
        sid,
        ib=_RaisingIB(),
        tp_target_price=Decimal("3.50"),
        alert_net_mid_threshold=Decimal("1.25"),
        polarity="DEBIT",
        sleep=sleeps.append,
        max_attempts=3,
        base_backoff=2.0,
    )

    # Adapter must be called exactly once — retries are wasted on a
    # terminal Gate-4 refusal.
    assert len(calls) == 1
    # No backoff sleeps were taken (loop exited on first attempt).
    assert sleeps == []
    assert result["tp_attached"] is False
    assert result["tp_refused_reason"] == "NAKED_SHORT_GUARD"
    # Alert still armed → session is in the terminal refused state
    # (protect.py's existing idiom: PROTECTED when alert armed, since
    # the operator retains the Risk Alert safety net).
    assert result["alert_armed"] is True
    assert result["state"] == "PROTECTED"

    con = duckdb.connect(str(db))
    try:
        events = con.execute(
            "SELECT kind FROM wizard_session_events WHERE session_id=?",
            [sid],
        ).fetchall()
    finally:
        con.close()
    kinds = [row[0] for row in events]
    assert "PROTECTION_TP_REFUSED" in kinds


def test_signed_combo_pricing_preserved_for_credit(tmp_path, monkeypatch):
    """CREDIT spreads have negative net prices — protect must not apply abs()."""
    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    sid = _init_session(db, state="FILLED")

    ib = _StubIB(
        tp_acks=[{"order_id": 9001, "perm_id": "p"}],
        alert_acks=[{"virtual_id": "alert-1"}],
    )

    # Negative signed target — a credit-spread close.
    signed_target = Decimal("-0.10")
    signed_threshold = Decimal("-0.45")

    protect.attach_protection(
        sid,
        ib=ib,
        tp_target_price=signed_target,
        alert_net_mid_threshold=signed_threshold,
        polarity="CREDIT",
        sleep=lambda _s: None,
    )

    # Target passed to IB must be the signed value (no abs() mangling).
    assert ib.tp_calls[0]["target_price"] == signed_target
    assert ib.alert_calls[0]["threshold"] == signed_threshold

    con = duckdb.connect(str(db))
    try:
        stored = con.execute(
            "SELECT tp_target_price, alert_net_mid_threshold FROM wizard_protection WHERE session_id=?",
            [sid],
        ).fetchone()
    finally:
        con.close()
    assert Decimal(str(stored[0])) == signed_target
    assert Decimal(str(stored[1])) == signed_threshold
