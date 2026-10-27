"""PR-10 T1 — pool readiness in /health, base-derived pool ids, base↔DB check."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from xenon.api import ib_pool as ib_pool_mod
from xenon.api.ib_pool import IBPool, check_client_id_base_mapping
from xenon.clients.ib_client import READINESS_LOST, READINESS_READY, READINESS_UNKNOWN
from xenon.api.routes import system as system_routes


def _fake_client(connected: bool, readiness: str) -> MagicMock:
    client = MagicMock()
    client.ib.isConnected.return_value = connected
    client.broker_readiness = readiness
    return client


def test_status_reports_readiness_separately_from_connected(monkeypatch):
    monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", "100")
    pool = IBPool()
    pool._clients["orders"] = _fake_client(True, READINESS_LOST)
    pool._clients["sync"] = _fake_client(True, READINESS_READY)
    pool._clients["data"] = _fake_client(False, READINESS_READY)  # dead socket: stale value ignored

    status = pool.status()

    assert status["orders"] == {"connected": True, "broker_readiness": READINESS_LOST, "client_id": 104, "fenced": False}
    assert status["sync"] == {"connected": True, "broker_readiness": READINESS_READY, "client_id": 103, "fenced": False}
    assert status["data"] == {"connected": False, "broker_readiness": READINESS_UNKNOWN, "client_id": 105, "fenced": False}


@pytest.mark.asyncio
async def test_connect_all_dials_base_offsets(monkeypatch):
    monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", "100")
    dialed = []

    def fake_connect(host, port, client_id, timeout):
        dialed.append(client_id)
        return _fake_client(True, READINESS_READY)

    monkeypatch.setattr(ib_pool_mod, "_connect_in_thread", fake_connect)
    monkeypatch.setattr(ib_pool_mod.asyncio, "sleep", AsyncMock())
    pool = IBPool()
    try:
        await pool.connect_all()
    finally:
        await pool.disconnect_all()
    assert dialed == [103, 104, 105]


@pytest.mark.asyncio
async def test_connect_all_keeps_the_startup_326_cause(monkeypatch):
    from xenon.clients.ib_client import ClientIdInUse

    monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", "100")

    def fake_connect(host, port, client_id, timeout):
        if client_id == 104:
            raise ClientIdInUse(client_id, "326")
        return _fake_client(True, READINESS_READY)

    monkeypatch.setattr(ib_pool_mod, "_connect_in_thread", fake_connect)
    monkeypatch.setattr(ib_pool_mod.asyncio, "sleep", AsyncMock())
    pool = IBPool()
    try:
        status = await pool.connect_all()
    finally:
        await pool.disconnect_all()
    assert status == {"sync": True, "orders": False, "data": True}
    assert isinstance(pool.connect_errors["orders"], ClientIdInUse)
    assert set(pool.connect_errors) == {"orders"}


def test_connect_in_thread_opens_pool_role_and_runs_round_trip(monkeypatch):
    calls = {}

    class FakeIBClient:
        def connect(self, **kwargs):
            calls["connect"] = kwargs

        def confirm_broker_ready(self):
            calls["probe"] = True
            return READINESS_READY

    monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", "100")
    monkeypatch.setattr(ib_pool_mod, "IBClient", FakeIBClient)
    ib_pool_mod._connect_in_thread("127.0.0.1", 4002, 104, 5)
    assert calls["connect"]["client_id"] == 104
    assert calls["connect"]["pool_role"] == "orders"
    assert calls["probe"] is True


def test_connect_in_thread_refuses_non_pool_ids(monkeypatch):
    monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", "100")
    monkeypatch.setattr(ib_pool_mod, "IBClient", MagicMock())
    with pytest.raises(ValueError, match="not a pool role id"):
        ib_pool_mod._connect_in_thread("127.0.0.1", 4002, 4, 5)  # prod's orders id


# ---------------------------------------------------------------------------
# base ↔ database mapping (A5)
# ---------------------------------------------------------------------------

_PROD = "postgresql+psycopg://u:p@db.example:5432/core_dev"
_DEV = "postgresql+psycopg://u:p@db.example:5432/core_test"


@pytest.mark.parametrize(
    ("base", "url", "test_session"),
    [
        ("0", _PROD, False),
        ("100", _DEV, False),
        ("200", "postgresql+psycopg://u:p@127.0.0.1/xenon_local", False),
        ("0", _DEV, True),  # scoped test mapping (pytest broker guard on)
        ("0", None, True),
        ("0", _PROD + "?sslmode=require&connect_timeout=5", False),
    ],
)
def test_mapping_accepts_safe_pairs(monkeypatch, base, url, test_session):
    monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", base)
    assert check_client_id_base_mapping(url, test_session=test_session) == int(base)


@pytest.mark.parametrize(
    ("base", "url", "test_session", "match"),
    [
        ("100", _PROD, False, "prod database"),
        ("100", _PROD, True, "prod database"),  # the test scope never relaxes the prod side
        ("0", _DEV, False, "non-prod database"),
        ("0", None, False, "non-prod database"),
        ("0", "not a url", False, "not a parseable"),
        ("100", "not a url", True, "not a parseable"),
        # F5 (Astra T1–T3): a query key that can re-route the connection is
        # refused whichever way it points; so is a URL without a database.
        ("100", _DEV + "?dbname=core_dev", False, "re-route"),
        ("0", _PROD + "?dbname=core_test", False, "re-route"),
        ("100", "postgresql+asyncpg://u@127.0.0.1/core_test?database=core_dev", False, "re-route"),
        ("0", "postgresql+asyncpg://u@127.0.0.1/core_dev?database=core_test", False, "re-route"),
        ("100", "postgresql+asyncpg://u@127.0.0.1/core_test?host=db.example", True, "re-route"),
        ("100", "postgresql+asyncpg://u@127.0.0.1/", False, "names no database"),
    ],
)
def test_mapping_refuses_unsafe_pairs(monkeypatch, base, url, test_session, match):
    monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", base)
    with pytest.raises(RuntimeError, match=match):
        check_client_id_base_mapping(url, test_session=test_session)


@pytest.mark.asyncio
async def test_lifespan_refuses_start_on_mismatch_before_gateway(monkeypatch):
    from xenon.api import server

    monkeypatch.delenv("XENON_API_TEST_MODE", raising=False)
    monkeypatch.setenv("XENON_IB_CLIENT_ID_BASE", "100")
    monkeypatch.setenv("DATABASE_URL", _PROD)
    ensure_gw = AsyncMock()
    monkeypatch.setattr(server, "ensure_ib_gateway", ensure_gw)
    pool_ctor = MagicMock()
    monkeypatch.setattr(server, "IBPool", pool_ctor)

    with pytest.raises(RuntimeError, match="refusing to start"):
        async with server.lifespan(server.app):
            pass
    ensure_gw.assert_not_awaited()
    pool_ctor.assert_not_called()


def test_health_exposes_broker_readiness(monkeypatch):
    from xenon.api import server

    async def fake_gateway():
        return {"port_listening": True}

    pool = IBPool()
    pool._clients["orders"] = _fake_client(True, READINESS_LOST)
    monkeypatch.setattr(system_routes, "check_ib_gateway", fake_gateway)
    monkeypatch.setattr(server, "ib_pool", pool)

    body = TestClient(server.app).get("/health").json()

    assert body["ib_pool"]["orders"]["connected"] is True
    assert body["ib_pool"]["orders"]["broker_readiness"] == READINESS_LOST
    assert body["ib_pool"]["sync"]["broker_readiness"] == READINESS_UNKNOWN
