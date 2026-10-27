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
