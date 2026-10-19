"""Wizard Risk Alert lifespan task (spec §2.9) — replaces the monitor daemon's
``wizard_stop_monitor`` handler.

Threshold rule is the daemon's: signed combo net-mid ``<=`` the stored
threshold crosses. A quote older than 2 minutes is the NO_QUOTE state and
never a trigger. The task never places, cancels or modifies an order.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from xenon.api.services import wizard_risk_alert as wra

NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)


def _session(sid="s1", ticker="AAPL", threshold="1.20"):
    return {
        "session_id": sid,
        "ticker": ticker,
        "payload": {"legs": []},
        "threshold": Decimal(threshold) if threshold is not None else None,
    }


class _Recorder:
    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    def __call__(self, session_id, kind, detail):
        self.events.append((session_id, kind, detail))


def _monitor(sessions, quotes, *, read_only=False, recorder=None):
    """Build a monitor with a fake mid. ``quotes`` maps session_id ->
    ComboQuote | None | Exception."""

    async def quote_fn(session):
        q = quotes[session["session_id"]]
        if isinstance(q, Exception):
            raise q
        return q

    return wra.RiskAlertMonitor(
        list_sessions_fn=lambda: list(sessions),
        quote_fn=quote_fn,
        record_event_fn=recorder or _Recorder(),
        read_only_fn=lambda: read_only,
        now_fn=lambda: NOW,
    )


def _q(mid, age_s=5):
    return wra.ComboQuote(mid=Decimal(mid), as_of=NOW - timedelta(seconds=age_s))


@pytest.fixture
def order_spy(monkeypatch):
    """Fail the test if any order entry point is reached."""
    calls: list[str] = []
    from xenon.api import server
    from xenon.execution import ib_place_order

    def _boom(name):
        def _inner(*a, **k):
            calls.append(name)
            raise AssertionError(f"risk alert called order function {name}")

        return _inner

    for name in ("main", "place_order", "place_combo_order"):
        if hasattr(ib_place_order, name):
            monkeypatch.setattr(ib_place_order, name, _boom(f"ib_place_order.{name}"))
    for name in dir(server):
        if name.startswith(("_orders_", "orders_", "_cancel", "_modify", "submit_combo")) and callable(
            getattr(server, name)
        ):
            monkeypatch.setattr(server, name, _boom(f"server.{name}"))
    return calls


def test_crossed_rule_matches_daemon():
    assert wra.crossed(Decimal("1.20"), Decimal("1.20")) is True
    assert wra.crossed(Decimal("1.19"), Decimal("1.20")) is True
    assert wra.crossed(Decimal("1.21"), Decimal("1.20")) is False
    # signed credit space: -0.90 is below -0.80
    assert wra.crossed(Decimal("-0.90"), Decimal("-0.80")) is True


def test_trigger_records_one_event_and_crossed_state(order_spy):
    rec = _Recorder()
    mon = _monitor([_session()], {"s1": _q("1.10")}, recorder=rec)
    summary = asyncio.run(mon.evaluate_once())
    assert summary["crossed"] == 1
    assert summary["orders_placed"] == 0
    assert [(s, k) for s, k, _ in rec.events] == [("s1", "RISK_ALERT_CROSSED")]
    assert rec.events[0][2]["quote"] == "1.10"
    assert rec.events[0][2]["threshold"] == "1.20"
    (alert,) = mon.alerts()
    assert alert["state"] == "CROSSED"
    assert alert["ticker"] == "AAPL"
    # Persisting condition does not re-record.
    asyncio.run(mon.evaluate_once())
    assert len(rec.events) == 1
    assert order_spy == []


def test_recover_then_recross_records_again():
    rec = _Recorder()
    quotes = {"s1": _q("1.10")}
    mon = _monitor([_session()], quotes, recorder=rec)
    asyncio.run(mon.evaluate_once())
    quotes["s1"] = _q("1.50")
    asyncio.run(mon.evaluate_once())
    assert mon.alerts() == []
    quotes["s1"] = _q("1.00")
    asyncio.run(mon.evaluate_once())
    assert len(rec.events) == 2


def test_no_trigger_above_threshold():
    rec = _Recorder()
    mon = _monitor([_session()], {"s1": _q("1.50")}, recorder=rec)
    summary = asyncio.run(mon.evaluate_once())
    assert summary["crossed"] == 0
    assert rec.events == []
    assert mon.alerts() == []
    assert mon.states()[0]["state"] == "OK"


@pytest.mark.parametrize(
    "quote",
    [
        None,
        RuntimeError("ib down"),
        # Below the threshold but 121 s old: must NOT trigger.
        wra.ComboQuote(mid=Decimal("0.50"), as_of=NOW - timedelta(seconds=121)),
        wra.ComboQuote(mid=Decimal("0.50"), as_of=None),
    ],
)
def test_stale_or_missing_quote_is_no_quote_state_never_trigger(quote):
    rec = _Recorder()
    mon = _monitor([_session()], {"s1": quote}, recorder=rec)
    summary = asyncio.run(mon.evaluate_once())
    assert summary["crossed"] == 0
    assert rec.events == []
    (alert,) = mon.alerts()
    assert alert["state"] == "NO_QUOTE"


def test_quote_at_exactly_two_minutes_is_still_fresh():
    rec = _Recorder()
    mon = _monitor([_session()], {"s1": _q("1.00", age_s=120)}, recorder=rec)
    asyncio.run(mon.evaluate_once())
    assert len(rec.events) == 1


def test_read_only_evaluates_but_never_writes(order_spy):
    rec = _Recorder()
    mon = _monitor([_session()], {"s1": _q("1.00")}, read_only=True, recorder=rec)
    summary = asyncio.run(mon.evaluate_once())
    assert summary["crossed"] == 1
    assert rec.events == []
    assert mon.alerts()[0]["state"] == "CROSSED"
    assert order_spy == []


def test_session_without_threshold_is_skipped():
    rec = _Recorder()
    mon = _monitor([_session(threshold=None)], {"s1": _q("0.10")}, recorder=rec)
    asyncio.run(mon.evaluate_once())
    assert rec.events == []
    assert mon.states() == []


def test_list_protected_sessions_filters_state_and_alert_flag(monkeypatch):
    """Only PROTECTED sessions with an active, alert-enabled protection are
    evaluated (the SQL filters PROTECTED + active; alert_enabled is checked
    here like the daemon did)."""
    from xenon.db.engine import get_sync_engine
    from xenon.db.queries import combo_wizard as cwq

    scope = {"broker": "IB", "account_env": "paper", "broker_account": "DU1234567"}
    engine = get_sync_engine()
    with engine.begin() as conn:
        for sid, state, enabled in (
            ("p-on", "PROTECTED", True),
            ("p-off", "PROTECTED", False),
            ("filled", "FILLED", True),
        ):
            cwq.create_session(conn, session_id=sid, ticker="SPY", state=state, payload={"legs": []}, **scope)
            cwq.upsert_protection(
                conn,
                sid,
                config={"alert_enabled": enabled, "alert_net_mid_threshold": "1.25"},
            )
    rows = wra.list_alert_sessions(scope)
    assert [r["session_id"] for r in rows] == ["p-on"]
    assert rows[0]["threshold"] == Decimal("1.25")


def test_lifespan_task_skipped_in_test_mode(monkeypatch):
    from xenon.api import server

    monkeypatch.setattr(server.app.state, "wizard_risk_alert_task", None, raising=False)
    server._maybe_start_wizard_risk_alert()
    assert getattr(server.app.state, "wizard_risk_alert_task", None) is None


def test_lifespan_task_starts_on_opt_in_and_cancels_cleanly(monkeypatch):
    from xenon.api import server

    monkeypatch.setattr(server, "_is_test_mode", lambda: False)
    monkeypatch.setattr(server.app.state, "trading_mode", "paper", raising=False)
    monkeypatch.setattr(server.app.state, "account", "DU1234567", raising=False)
    monkeypatch.setattr(server.app.state, "mode_verified", True, raising=False)
    monkeypatch.setattr(server, "ib_pool", object())
    ticks: list[int] = []

    async def fake_eval(self):
        ticks.append(1)
        return {"checked": 0, "crossed": 0, "orders_placed": 0}

    monkeypatch.setattr(wra.RiskAlertMonitor, "evaluate_once", fake_eval)

    async def run():
        server._maybe_start_wizard_risk_alert(interval_s=0.01)
        task = server.app.state.wizard_risk_alert_task
        await asyncio.sleep(0.05)
        await server._stop_wizard_risk_alert()
        return task

    task = asyncio.run(run())
    assert task.cancelled() or task.done()
    assert ticks
    assert server.app.state.wizard_risk_alert_task is None


def _client(monkeypatch, alerts):
    from xenon.api import server

    class _Mon:
        def alerts(self):
            return alerts

        last_run_at = "2026-10-20T15:00:00+00:00"

    monkeypatch.setattr(server.app.state, "wizard_risk_alert", _Mon(), raising=False)
    return TestClient(server.app)


def test_get_wizard_alerts_returns_live_states_and_persisted_events(monkeypatch):
    from xenon.api import server
    from xenon.db.engine import get_sync_engine
    from xenon.db.queries import combo_wizard as cwq

    scope = server._resolve_scope_kwargs()
    engine = get_sync_engine()
    with engine.begin() as conn:
        cwq.create_session(conn, session_id="s-ev", ticker="QQQ", state="PROTECTED", payload={}, **scope)
        cwq.record_event(conn, session_id="s-ev", kind="RISK_ALERT_CROSSED", detail={"quote": "1.0"})
        cwq.record_event(conn, session_id="s-ev", kind="SUBMITTED", detail={})
    live = [{"session_id": "s-ev", "ticker": "QQQ", "state": "NO_QUOTE"}]
    body = _client(monkeypatch, live).get("/wizard/alerts").json()
    assert body["alerts"] == live
    assert body["monitor"]["running"] is True
    assert [(e["session_id"], e["kind"], e["ticker"]) for e in body["events"]] == [
        ("s-ev", "RISK_ALERT_CROSSED", "QQQ")
    ]


def test_get_wizard_alerts_when_monitor_not_running(monkeypatch):
    from xenon.api import server

    monkeypatch.setattr(server.app.state, "wizard_risk_alert", None, raising=False)
    body = TestClient(server.app).get("/wizard/alerts").json()
    assert body["alerts"] == []
    assert body["monitor"]["running"] is False


class _Tick:
    def __init__(self, bid, ask, age_s):
        self.bid, self.ask, self.bidSize, self.askSize = bid, ask, 10, 10
        self.time = datetime.now(timezone.utc) - timedelta(seconds=age_s)


class _FakeIB:
    def __init__(self, ticks):
        self.ticks = ticks
        self.calls: list[bool] = []

    def get_quote(self, contract, snapshot=False, generic_ticks=""):
        self.calls.append(snapshot)
        return self.ticks[contract.conId]


def _seed_legs(sid="s-q"):
    from xenon.db.engine import get_sync_engine
    from xenon.db.queries import combo_wizard as cwq

    legs = [
        {"conId": 1, "symbol": "SPY", "expiry": "20261120", "strike": 500, "right": "C", "action": "BUY", "ratio": 1},
        {"conId": 2, "symbol": "SPY", "expiry": "20261120", "strike": 510, "right": "C", "action": "SELL", "ratio": 1},
    ]
    with get_sync_engine().begin() as conn:
        cwq.create_session(conn, session_id=sid, ticker="SPY", state="PROTECTED", payload={"legs": legs})
    return sid


def test_pool_quote_uses_snapshots_and_reports_oldest_tick():
    sid = _seed_legs()
    ib = _FakeIB({1: _Tick(5.0, 5.2, 10), 2: _Tick(2.0, 2.2, 40)})
    q = wra._combo_quote_with_client(ib, sid)
    assert ib.calls == [True, True]  # snapshot only, no streaming line held
    assert q.mid == Decimal("3.0")  # 5.1 - 2.1, signed debit
    assert q.as_of == ib.ticks[2].time


def test_pool_quote_older_than_two_minutes_is_none():
    sid = _seed_legs()
    ib = _FakeIB({1: _Tick(5.0, 5.2, 10), 2: _Tick(2.0, 2.2, 121)})
    assert wra._combo_quote_with_client(ib, sid) is None


# --- Freshness clock is read AFTER acquisition (review PR-211 P1) ----------


class _Clock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t


def test_quote_received_during_acquisition_triggers(order_spy):
    """Acquisition advances the clock ~2 s and the quote is stamped at
    completion; it must count as fresh → CROSSED and one event."""
    clock = _Clock(NOW)
    rec = _Recorder()

    async def quote_fn(session):
        clock.t = clock.t + timedelta(seconds=2)
        return wra.ComboQuote(mid=Decimal("1.00"), as_of=clock.t)

    mon = wra.RiskAlertMonitor(
        list_sessions_fn=lambda: [_session()],
        quote_fn=quote_fn,
        record_event_fn=rec,
        read_only_fn=lambda: False,
        now_fn=clock,
    )
    asyncio.run(mon.evaluate_once())
    assert mon.alerts()[0]["state"] == "CROSSED"
    assert len(rec.events) == 1
    assert order_spy == []


def test_tick_slightly_newer_than_clock_is_fresh():
    rec = _Recorder()
    q = wra.ComboQuote(mid=Decimal("1.00"), as_of=NOW + timedelta(seconds=1))
    mon = _monitor([_session()], {"s1": q}, recorder=rec)
    asyncio.run(mon.evaluate_once())
    assert len(rec.events) == 1


class _LiveStampIB:
    """get_quote stamps ticker.time with the real clock DURING the call, like
    ib_async does at receive time."""

    def __init__(self, prices):
        self.prices = prices

    def get_quote(self, contract, snapshot=False, generic_ticks=""):
        import time as _time

        _time.sleep(0.01)
        bid, ask = self.prices[contract.conId]
        t = _Tick(bid, ask, 0)
        t.time = datetime.now(timezone.utc)
        return t


def test_pool_quote_stamped_during_get_quote_crosses_end_to_end(order_spy):
    sid = _seed_legs("s-live")
    ib = _LiveStampIB({1: (5.0, 5.2), 2: (2.0, 2.2)})

    async def quote_fn(session):
        return wra._combo_quote_with_client(ib, session["session_id"])

    rec = _Recorder()
    mon = wra.RiskAlertMonitor(
        list_sessions_fn=lambda: [_session(sid, "SPY", "3.50")],
        quote_fn=quote_fn,
        record_event_fn=rec,
        read_only_fn=lambda: False,
    )
    asyncio.run(mon.evaluate_once())
    (alert,) = mon.alerts()
    assert alert["state"] == "CROSSED"
    assert Decimal(alert["quote"]) == Decimal("3.0")
    assert [(s, k) for s, k, _ in rec.events] == [(sid, "RISK_ALERT_CROSSED")]
    assert order_spy == []


def test_builder_rechecks_oldest_leg_against_completion_time():
    """Leg 1 is fresh when fetched, but later acquisition pushes completion
    past 120 s after leg 1's tick → no quote."""
    from xenon.execution.combo_wizard.combo_quote_source import build_default_quote_fn

    sid = _seed_legs("s-age")
    clock = _Clock(NOW)

    class _SlowIB:
        def get_quote(self, contract, snapshot=False, generic_ticks=""):
            t = _Tick(5.0, 5.2, 0)
            t.time = clock.t
            clock.t = clock.t + timedelta(seconds=61)
            return t

    assert build_default_quote_fn(lambda: _SlowIB(), ttl_s=120, now_fn=clock)(sid) is None

    clock.t = NOW

    class _FastIB(_SlowIB):
        def get_quote(self, contract, snapshot=False, generic_ticks=""):
            t = _Tick(5.0, 5.2, 0)
            clock.t = clock.t + timedelta(seconds=2)
            t.time = clock.t
            return t

    assert build_default_quote_fn(lambda: _FastIB(), ttl_s=120, now_fn=clock)(sid) is not None
