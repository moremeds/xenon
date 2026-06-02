"""Tests for monitor_daemon.handlers.wizard_stop_monitor.

Polls wizard sessions in PROTECTED state; for each, compares the current
combo mid against the stored alert threshold and emits a session event +
notification when the threshold is crossed. Never places close orders —
Risk Alert is assisted-exit; the operator confirms.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

# Phase 3: opt out of Phase 2 BoundEngine binding — this file owns its
# own TRUNCATE-based teardown that needs committed cross-test state.
pytestmark = pytest.mark.committed_db
from sqlalchemy import create_engine, text

from xenon.db.queries import combo_wizard as cwq
from xenon.execution import orders_store
from xenon.monitor_daemon.handlers.wizard_stop_monitor import WizardStopMonitorHandler

# --------------------------------------------------------------------------
# Postgres helpers
# --------------------------------------------------------------------------

from xenon._test_db import sync_test_db_url as _sync_url  # worker-aware URL resolver


def _pg_engine():
    return create_engine(_sync_url(), pool_pre_ping=True)


def _cleanup(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE xenon.wizard_events CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_protection CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_combo_attempts CASCADE"))
        conn.execute(text("TRUNCATE xenon.wizard_sessions CASCADE"))


@pytest.fixture(autouse=True)
def _setup_pg(monkeypatch):
    """Point get_sync_engine() at the test database and clean tables."""
    monkeypatch.setenv("DATABASE_URL", _sync_url())
    # Tests seed rows with default (legacy_unknown) scope. Clear scope env
    # vars so the handler doesn't filter them out by inherited shell/.env state.
    monkeypatch.delenv("XENON_BROKER", raising=False)
    monkeypatch.delenv("XENON_TRADING_MODE", raising=False)
    monkeypatch.delenv("XENON_BROKER_ACCOUNT", raising=False)
    import xenon.db.engine as eng_mod

    monkeypatch.setattr(eng_mod, "_sync_engine", None)

    engine = _pg_engine()
    _cleanup(engine)
    engine.dispose()
    yield
    engine = _pg_engine()
    _cleanup(engine)
    engine.dispose()


# --------------------------------------------------------------------------
# Seed helper
# --------------------------------------------------------------------------


def _seed_protected(db_path: Path, *, threshold: Decimal, polarity: str = "DEBIT") -> str:
    sid = f"wiz-{uuid.uuid4().hex[:12]}"
    payload = {
        "symbol": "AAPL",
        "type": "combo",
        "legs": [
            {"conId": 1001, "action": "BUY", "ratio": 1},
            {"conId": 1002, "action": "SELL", "ratio": 1},
        ],
    }
    config = {
        "tp_enabled": True,
        "tp_target_price": "3.50",
        "alert_enabled": True,
        "alert_net_mid_threshold": str(threshold),
    }
    engine = _pg_engine()
    with engine.begin() as conn:
        cwq.create_session(
            conn,
            session_id=sid,
            ticker="AAPL",
            state="PROTECTED",
            structure_name="Bull Call Spread",
            intent="OPEN",
            payload=payload,
        )
        cwq.upsert_protection(
            conn,
            sid,
            protection_type="combo_tp_alert",
            config=config,
            state="active",
        )
    engine.dispose()
    return sid


def test_handler_identity():
    h = WizardStopMonitorHandler()
    assert h.name == "wizard_stop_monitor"
    assert h.interval_seconds > 0
    assert h.requires_market_hours is True


def test_emits_event_when_threshold_crossed_debit(tmp_path, monkeypatch):
    """DEBIT polarity: Risk Alert fires when combo net mid falls BELOW threshold
    (the long spread has decayed into the alert zone)."""
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid = _seed_protected(tmp_path, threshold=Decimal("1.25"), polarity="DEBIT")

    quotes = {sid: Decimal("1.00")}  # below threshold → cross
    events: list[dict] = []

    h = WizardStopMonitorHandler(
        quote_fn=lambda session_id: quotes.get(session_id),
        notify_fn=lambda payload: events.append(payload),
    )
    result = h.execute()

    assert result["checked"] == 1
    assert result["crossed"] == 1
    assert len(events) == 1
    assert events[0]["session_id"] == sid
    # The popup copy must say Risk Alert → Assisted Exit, not stop-loss.
    body = (events[0].get("body") or "").lower()
    assert "risk alert" in body or "assisted exit" in body
    assert "stop-loss" not in body and "stop loss" not in body

    engine = _pg_engine()
    with engine.connect() as conn:
        kinds = [
            r[0]
            for r in conn.execute(
                text("SELECT kind FROM xenon.wizard_events WHERE session_id = :sid"),
                {"sid": sid},
            ).fetchall()
        ]
    engine.dispose()
    assert any("RISK_ALERT" in k for k in kinds)


def test_no_event_when_threshold_not_crossed(tmp_path, monkeypatch):
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid = _seed_protected(tmp_path, threshold=Decimal("1.25"), polarity="DEBIT")

    quotes = {sid: Decimal("2.00")}  # well above threshold
    events: list[dict] = []

    h = WizardStopMonitorHandler(
        quote_fn=lambda _sid: quotes.get(_sid),
        notify_fn=lambda payload: events.append(payload),
    )
    result = h.execute()
    assert result["checked"] == 1
    assert result["crossed"] == 0
    assert events == []


def test_handler_does_not_place_close_orders(tmp_path, monkeypatch):
    """Risk Alert is assisted-exit: operator confirms. The handler must not
    auto-place any order even when the threshold is crossed."""
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    _seed_protected(tmp_path, threshold=Decimal("1.25"))

    quotes_crossed = Decimal("1.00")

    h = WizardStopMonitorHandler(
        quote_fn=lambda _sid: quotes_crossed,
        notify_fn=lambda _p: None,
    )
    result = h.execute()
    # No orders_placed key — and if present, must be zero.
    assert result.get("orders_placed", 0) == 0


def test_handler_idempotent_within_same_run(tmp_path, monkeypatch):
    """Calling execute() twice in a row with the threshold still crossed must
    not emit duplicate events — we record that the alert has fired."""
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    sid = _seed_protected(tmp_path, threshold=Decimal("1.25"))

    quotes_crossed = Decimal("1.00")
    events: list[dict] = []

    h = WizardStopMonitorHandler(
        quote_fn=lambda _sid: quotes_crossed,
        notify_fn=lambda p: events.append(p),
    )
    h.execute()
    h.execute()

    assert len(events) == 1  # only fired once


def test_handler_registered_in_run_py():
    """Sanity-check that run.py's create_daemon registers the new handler."""
    # Patch out daemon state loading so we don't touch the real filesystem.
    import unittest.mock as mock

    from xenon.monitor_daemon import run as run_mod

    with mock.patch.object(run_mod.MonitorDaemon, "load_state", lambda self: None):
        daemon = run_mod.create_daemon()
    names = {h.name for h in daemon.handlers}
    assert "wizard_stop_monitor" in names
