"""Order routes return 503 when trading mode is unverified."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_with_mismatch(monkeypatch):
    """Boot in test mode with mode=live but a paper-prefixed account."""
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    import xenon.api.trading_mode as tm

    importlib.reload(tm)
    import xenon.api.server as server

    importlib.reload(server)
    monkeypatch.setattr(server, "_get_managed_account_for_health", lambda: "DU1111111")
    with TestClient(server.app) as c:
        yield c


@pytest.fixture
def client_with_match(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    import xenon.api.trading_mode as tm

    importlib.reload(tm)
    import xenon.api.server as server

    importlib.reload(server)
    monkeypatch.setattr(server, "_get_managed_account_for_health", lambda: "DU1111111")
    with TestClient(server.app) as c:
        yield c


@pytest.mark.parametrize(
    "path",
    [
        "/orders/refresh",
        "/orders/place",
        "/orders/cancel",
        "/orders/modify",
    ],
)
def test_orders_routes_blocked_on_mismatch(client_with_mismatch, path):
    r = client_with_mismatch.post(path, json={})
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert "trading mode" in detail.lower()
    assert "live" in detail.lower()
    assert "DU1111111" in detail


def test_orders_refresh_passes_when_verified(client_with_match):
    """Sanity: when mode matches, the guard does not block.

    /orders/refresh in test_mode short-circuits to {"status": "ok", "orders": []}
    so we get past the guard without needing real IB.
    """
    r = client_with_match.post("/orders/refresh")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "orders": []}
