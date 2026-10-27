"""IB quote / qualify deadline and upstream-lost detection (incident 2026-10-26).

The gateway lost IB servers but kept its API port open; `qualifyContracts`
never answered, the data-role call held the role lock forever and
`GET /orders/quote` (and the Futu SIMULATE quote gate) hung > 150 s.
"""

from __future__ import annotations

import asyncio
import threading
import time
from decimal import Decimal

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from xenon.api import ib_pool as ib_pool_mod
from xenon.api.ib_pool import IBDataTimeout, IBPool, IBUpstreamLost, call_data_bounded
from xenon.api.services import order_entry
from xenon.clients.ib_client import READINESS_LOST, READINESS_READY, READINESS_UNKNOWN, IBClient
from xenon.execution.preflight import ReasonCode


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_QUOTE_TOKEN_SECRET", "c" * 64)
    monkeypatch.setenv("XENON_IB_QUOTE_TIMEOUT_S", "0.5")


class _FakeIB:
    def __init__(self):
        self.RequestTimeout = 0

    def isConnected(self):
        return True


class _Ticker:
    bid, ask, bidSize, askSize = 1.2, 1.3, 4, 5


class _HangingClient:
    """qualify_contract blocks until `release` is set (a gateway that never
    answers and an ib_async call that ignores its timeout)."""

    def __init__(self, readiness=READINESS_READY):
        self.ib = _FakeIB()
        self.broker_readiness = readiness
        self.readiness_code = None
        self.release = threading.Event()
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.seen_request_timeout = None
        self.pumped = 0

    def pump_events(self):
        self.pumped += 1

    def qualify_contract(self, contract):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.seen_request_timeout = self.ib.RequestTimeout
        try:
            self.release.wait(10)
        finally:
            self.active -= 1
        return contract

    def get_quote(self, contract, snapshot=True):
        return _Ticker()


def _pool_with(client) -> IBPool:
    pool = IBPool()
    pool._clients["data"] = client
    pool._connected["data"] = True
    return pool


@pytest.fixture
def server_pool(monkeypatch):
    from xenon.api import server

    def _install(pool):
        monkeypatch.setattr(server, "ib_pool", pool)
        return pool

    return _install


# --- Task 1: deadline ---------------------------------------------------------


def test_quote_hang_answers_503_quote_unavailable_within_deadline(server_pool):
    client = _HangingClient()
    pool = server_pool(_pool_with(client))
    started = time.monotonic()
    with pytest.raises(HTTPException) as err:
        asyncio.run(order_entry._fetch_quote_snapshot("SPY", 756733))
    elapsed = time.monotonic() - started
    client.release.set()
    assert err.value.status_code == 503
    assert err.value.reason_code == ReasonCode.QUOTE_UNAVAILABLE
    assert elapsed < 2.0, elapsed
    assert not pool._locks["data"].locked()


def test_abandoned_call_fences_role_no_second_call_until_it_returns(server_pool):
    client = _HangingClient()
    pool = server_pool(_pool_with(client))
    with pytest.raises(HTTPException):
        asyncio.run(order_entry._fetch_quote_snapshot("SPY", 1))
    assert pool.is_fenced("data") and pool.status()["data"]["fenced"] is True

    # While the abandoned call still holds the client: fail fast, never a
    # second call on the same client.
    started = time.monotonic()
    with pytest.raises(HTTPException) as err:
        asyncio.run(order_entry._fetch_quote_snapshot("SPY", 1))
    assert time.monotonic() - started < 0.3
    assert err.value.reason_code == ReasonCode.QUOTE_UNAVAILABLE
    assert client.calls == 1 and client.max_active == 1

    # The worker returns → fence clears → the role serves again.
    client.release.set()
    deadline = time.monotonic() + 2
    while pool.is_fenced("data") and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not pool.is_fenced("data")
    snap = asyncio.run(order_entry._fetch_quote_snapshot("SPY", 1))
    assert snap["bid"] == Decimal("1.2") and client.calls == 2 and client.max_active == 1


def test_in_thread_request_timeout_is_bounded_and_restored(server_pool):
    client = _HangingClient()
    client.release.set()
    server_pool(_pool_with(client))
    asyncio.run(order_entry._fetch_quote_snapshot("SPY", 1))
    assert 0 < client.seen_request_timeout <= 0.5
    assert client.ib.RequestTimeout == 0


def test_ib_async_timeout_in_worker_maps_to_data_timeout():
    class _Client(_HangingClient):
        def qualify_contract(self, contract):
            raise asyncio.TimeoutError()

    pool = _pool_with(_Client())

    def job(c):
        return c.qualify_contract(None)

    with pytest.raises(IBDataTimeout):
        asyncio.run(call_data_bounded(pool, job))
    assert not pool.is_fenced("data")


def test_deadline_during_reconnect_releases_the_role_lock(monkeypatch):
    pool = IBPool()  # data role not connected → acquire reconnects

    async def slow_reconnect(role):
        await asyncio.sleep(5)
        return True

    monkeypatch.setattr(pool, "_reconnect", slow_reconnect)

    async def go():
        with pytest.raises(IBDataTimeout):
            await call_data_bounded(pool, lambda c: None, timeout=0.2)
        return pool._locks["data"].locked()

    assert asyncio.run(go()) is False


def test_quote_route_timeout_is_top_level_reason_code(monkeypatch):
    from xenon.api.server import app

    async def boom(ticker, con_id):
        raise order_entry._ib_data_error(IBDataTimeout("IB data call did not finish within 10s"))

    monkeypatch.setattr(order_entry, "_fetch_quote_snapshot", boom)
    resp = TestClient(app).get("/orders/quote", params={"ticker": "SPY", "con_id": 1})
    assert resp.status_code == 503
    assert resp.json()["reason_code"] == "QUOTE_UNAVAILABLE"


def test_quote_gate_refuses_place_on_timeout(server_pool):
    """The place quote gate (`_quote_payload_for`) refuses — no order is sent."""
    client = _HangingClient()
    server_pool(_pool_with(client))
    payload, con_id, verdict, status = asyncio.run(
        order_entry._quote_payload_for({"symbol": "SPY", "type": "stock", "con_id": 1})
    )
    client.release.set()
    assert payload is None and status == 503
    assert verdict.accept is False and verdict.reason_code == ReasonCode.QUOTE_UNAVAILABLE


# --- Task 2: upstream lost ----------------------------------------------------


def test_upstream_lost_fails_fast_with_ib_broker_disconnected(server_pool):
    client = _HangingClient(readiness=READINESS_LOST)
    client.readiness_code = 1100
    server_pool(_pool_with(client))
    started = time.monotonic()
    payload, _, verdict, status = asyncio.run(
        order_entry._quote_payload_for({"symbol": "SPY", "type": "stock", "con_id": 1})
    )
    assert time.monotonic() - started < 0.3
    assert client.calls == 0 and client.pumped == 1
    assert payload is None and status == 503
    assert verdict.reason_code == ReasonCode.IB_BROKER_DISCONNECTED


def test_pump_delivers_buffered_1100_before_the_check():
    client = _HangingClient()

    def pump():
        client.broker_readiness = READINESS_LOST

    client.pump_events = pump
    with pytest.raises(IBUpstreamLost):
        asyncio.run(call_data_bounded(_pool_with(client), lambda c: c.qualify_contract(None)))
    assert client.calls == 0


@pytest.mark.parametrize(
    ("code", "state"),
    [(1100, READINESS_LOST), (2110, READINESS_LOST), (1102, READINESS_READY), (1101, READINESS_UNKNOWN)],
)
def test_ib_client_records_readiness_code_and_since(code, state):
    c = IBClient()
    c._set_readiness(READINESS_READY if state != READINESS_READY else READINESS_LOST)
    before = time.time()
    c._on_error(-1, code, "connectivity")
    assert c.broker_readiness == state
    assert c.readiness_code == code
    assert c.readiness_since >= before


class _StateClient:
    def __init__(self, readiness, code=None, since=1_800_000_000.0):
        self.ib = _FakeIB()
        self.broker_readiness = readiness
        self.readiness_code = code
        self.readiness_since = since


def test_pool_upstream_status_lost_wins():
    pool = IBPool()
    pool._clients = {
        "sync": _StateClient(READINESS_READY, 1102, 1_800_000_000.0),
        "data": _StateClient(READINESS_LOST, 1100, 1_800_000_100.0),
    }
    up = pool.upstream_status()
    assert up["state"] == "lost" and up["last_code"] == 1100
    assert up["since"].startswith("2027-01-15T08:")


def test_pool_upstream_status_connected_and_unknown():
    pool = IBPool()
    pool._clients = {"sync": _StateClient(READINESS_READY), "data": _StateClient(READINESS_UNKNOWN)}
    assert pool.upstream_status()["state"] == "connected"
    pool._clients = {"data": _StateClient(READINESS_UNKNOWN)}
    assert pool.upstream_status() == {"state": "unknown", "since": pool.upstream_status()["since"], "last_code": None}
    assert IBPool().upstream_status() == {"state": "unknown", "since": None, "last_code": None}


def _health(monkeypatch, pool, halted=False):
    from xenon.api import server
    from xenon.api.routes import system as system_routes

    async def _gw():
        return {"port_listening": True}

    class _Owner:
        def status(self):
            return {"halted": halted}

    monkeypatch.setattr(system_routes, "check_ib_gateway", _gw)
    monkeypatch.setattr(server, "ib_pool", pool)
    monkeypatch.setattr(server, "order_owner", _Owner())
    return TestClient(server.app).get("/health").json()


def test_health_reports_upstream_lost_as_degraded(monkeypatch):
    pool = IBPool()
    pool._clients = {"data": _StateClient(READINESS_LOST, 2110)}
    body = _health(monkeypatch, pool)
    assert body["status"] == "degraded"
    assert body["ib_gateway"]["upstream"]["state"] == "lost"
    assert body["ib_gateway"]["upstream"]["last_code"] == 2110
    assert body["ib_gateway"]["port_listening"] is True


def test_health_orders_halted_wins_over_degraded(monkeypatch):
    pool = IBPool()
    pool._clients = {"data": _StateClient(READINESS_LOST, 1100)}
    assert _health(monkeypatch, pool, halted=True)["status"] == "orders_halted"


def test_health_ok_when_upstream_connected(monkeypatch):
    pool = IBPool()
    pool._clients = {"data": _StateClient(READINESS_READY, 1102)}
    body = _health(monkeypatch, pool)
    assert body["status"] == "ok" and body["ib_gateway"]["upstream"]["state"] == "connected"


def test_quote_timeout_env_parsing(monkeypatch):
    monkeypatch.setenv("XENON_IB_QUOTE_TIMEOUT_S", "abc")
    assert ib_pool_mod.ib_quote_timeout_s() == 10.0
    monkeypatch.setenv("XENON_IB_QUOTE_TIMEOUT_S", "-1")
    assert ib_pool_mod.ib_quote_timeout_s() == 10.0
    monkeypatch.setenv("XENON_IB_QUOTE_TIMEOUT_S", "3")
    assert ib_pool_mod.ib_quote_timeout_s() == 3.0


# --- Astra PR #254 review fixes ------------------------------------------------


def test_abandoned_real_reconnect_fences_role_no_duplicate_connect(monkeypatch):
    """Finding 1: the executor-backed reconnect (`reconnect_role` →
    `_connect_owned` → `_connect_in_thread`) abandoned by a deadline fences
    the role; a second caller never queues another connect on the clientId."""
    gate = threading.Event()
    made: list = []

    def fake_connect(host, port, client_id, timeout=5):
        made.append(client_id)
        if len(made) == 1:
            gate.wait(10)
        return _StateClient(READINESS_READY)

    monkeypatch.setattr(ib_pool_mod, "_connect_in_thread", fake_connect)
    pool = IBPool()  # data not connected → acquire reconnects on the worker

    async def go():
        with pytest.raises(IBDataTimeout):
            await call_data_bounded(pool, lambda c: "x", timeout=0.2)
        assert pool.is_fenced("data")
        assert not pool._locks["data"].locked()
        with pytest.raises(ib_pool_mod.IBRoleFenced):
            await call_data_bounded(pool, lambda c: "x", timeout=0.2)
        gate.set()
        for _ in range(300):
            if not pool.is_fenced("data"):
                break
            await asyncio.sleep(0.01)
        assert not pool.is_fenced("data")
        return await call_data_bounded(pool, lambda c: c, timeout=1)

    client = asyncio.run(go())
    assert len(made) == 1  # one connect only; the third call reuses it
    assert pool._clients["data"] is client


def _spec():
    return {"con_id": 123, "sym": "SPY", "right": "C", "exp": "20261120", "strike": 500.0}


class _MetaClient(_StateClient):
    def __init__(self, readiness):
        super().__init__(readiness)
        self.detail_calls = 0
        self.seen_timeout = None

    def get_contract_details(self, contract):
        self.detail_calls += 1
        self.seen_timeout = self.ib.RequestTimeout
        return []


def _run_resolver(pool):
    from xenon.api.services import futu_observation as fo

    fo._IB_META_CACHE.clear()
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        return fo.make_ib_meta_resolver(loop, lambda: pool)([_spec()])
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(2)


def test_futu_meta_resolver_refuses_on_lost_upstream_without_ib_call():
    """Finding 2: upstream lost → unresolved at once, no IB call, no fence."""
    client = _MetaClient(READINESS_LOST)
    pool = _pool_with(client)
    assert _run_resolver(pool) == [None]
    assert client.detail_calls == 0 and not pool.is_fenced("data")


def test_futu_meta_resolver_bounds_the_worker_request_timeout():
    """Finding 2: the worker's IB requests carry a RequestTimeout below the
    caller's META_TIMEOUT_S (was 0 = wait forever)."""
    from xenon.api.services import futu_observation as fo

    client = _MetaClient(READINESS_READY)
    pool = _pool_with(client)
    assert _run_resolver(pool) == [None]
    assert client.detail_calls == 1
    assert 0 < client.seen_timeout < fo.META_TIMEOUT_S
    assert client.ib.RequestTimeout == 0


def _hist_request(pool):
    from types import SimpleNamespace

    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ib_pool=pool)))


@pytest.mark.parametrize("route", ["head", "bars"])
def test_historical_lock_wait_is_under_the_deadline(route, monkeypatch):
    """Finding 3: lock wait counts against the qualify deadline."""
    from xenon.api.routes import historical as hist

    monkeypatch.setenv("XENON_IB_QUOTE_TIMEOUT_S", "0.2")
    pool = _pool_with(_StateClient(READINESS_READY))
    spec = hist.ContractSpec(sec_type="STK", symbol="SPY")
    req = (
        hist.HeadTimestampRequest(contract=spec)
        if route == "head"
        else hist.HistoricalBarsRequest(contract=spec, duration="1 D")
    )
    fn = hist.head_timestamp if route == "head" else hist.historical_bars

    async def go():
        await pool._locks["data"].acquire()  # a stuck holder
        try:
            with pytest.raises(HTTPException) as err:
                await asyncio.wait_for(fn(req, _hist_request(pool)), 2)
            return err.value.status_code
        finally:
            pool._locks["data"].release()

    assert asyncio.run(go()) == 503


def test_historical_blocked_qualify_times_out_and_fences(monkeypatch):
    """Finding 3: a qualify that ignores its worker bound is abandoned at the
    deadline and fences the role."""
    from xenon.api.routes import historical as hist

    monkeypatch.setenv("XENON_IB_QUOTE_TIMEOUT_S", "0.2")
    client = _StateClient(READINESS_READY)
    gate = threading.Event()
    client.qualify_contracts = lambda *cs: gate.wait(10)
    pool = _pool_with(client)
    req = hist.HeadTimestampRequest(contract=hist.ContractSpec(sec_type="STK", symbol="SPY"))

    async def go():
        with pytest.raises(HTTPException) as err:
            await asyncio.wait_for(hist.head_timestamp(req, _hist_request(pool)), 2)
        return err.value.status_code

    try:
        assert asyncio.run(go()) == 503
        assert pool.is_fenced("data") and not pool._locks["data"].locked()
    finally:
        gate.set()


def test_upstream_last_code_follows_event_time_not_state_entry(monkeypatch):
    """Finding 4: data 1100 @100, sync 1100 @200, data 2110 @300 (still lost)
    → last_code 2110; since = first state entry (100)."""
    import xenon.clients.ib_client as ibc

    now = {"t": 50.0}
    monkeypatch.setattr(ibc.time, "time", lambda: now["t"])
    data, sync = IBClient(), IBClient()
    for t, client, code in ((100.0, data, 1100), (200.0, sync, 1100), (300.0, data, 2110)):
        now["t"] = t
        client._on_error(-1, code, "connectivity")
    monkeypatch.undo()
    pool = IBPool()
    pool._clients = {"data": data, "sync": sync}
    monkeypatch.setattr(pool, "is_connected", lambda role: True)
    up = pool.upstream_status()
    assert up["state"] == "lost" and up["last_code"] == 2110
    assert up["since"].startswith("1970-01-01T00:01:40")


def test_wizard_quote_fn_is_bounded_and_refuses_lost_upstream(monkeypatch):
    """Review item 5: the background combo snapshot runs through the bounded
    wrapper (worker RequestTimeout, upstream fast-fail)."""
    from xenon.api.services import wizard_risk_alert as wra

    seen = {}

    def fake_combo(client, session_id):
        seen["timeout"] = client.ib.RequestTimeout
        return None

    monkeypatch.setattr(wra, "_combo_quote_with_client", fake_combo)
    ready = _pool_with(_StateClient(READINESS_READY))
    assert asyncio.run(wra.build_pool_quote_fn(ready)({"session_id": "s1"})) is None
    assert 0 < seen["timeout"] <= wra.QUOTE_TIMEOUT_S

    seen.clear()
    lost = _pool_with(_StateClient(READINESS_LOST, 1100))
    with pytest.raises(IBUpstreamLost):
        asyncio.run(wra.build_pool_quote_fn(lost)({"session_id": "s1"}))
    assert seen == {}
