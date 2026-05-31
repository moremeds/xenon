"""/health surfaces trading_mode, account, mode_verified."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_in_test_mode(monkeypatch):
    """Boot the FastAPI app in test mode with mode=paper and a fake managed account."""
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    import xenon.api.trading_mode as tm

    importlib.reload(tm)
    import xenon.api.server as server

    importlib.reload(server)
    monkeypatch.setattr(server, "_get_managed_account_for_health", lambda: "DU9999999")
    with TestClient(server.app) as c:
        yield c


def test_health_includes_trading_mode_fields(client_in_test_mode):
    r = client_in_test_mode.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["trading_mode"] == "paper"
    assert body["account"] == "DU***9999"
    assert body["mode_verified"] is True


def test_health_mode_verified_false_on_mismatch(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    import xenon.api.trading_mode as tm

    importlib.reload(tm)
    import xenon.api.server as server

    importlib.reload(server)
    monkeypatch.setattr(server, "_get_managed_account_for_health", lambda: "DU9999999")
    with TestClient(server.app) as c:
        r = c.get("/health")
        body = r.json()
        assert body["trading_mode"] == "live"
        assert body["account"] == "DU***9999"
        assert body["mode_verified"] is False


def test_lifespan_account_state_falls_back_to_env_without_verifying(monkeypatch):
    """Gateway-down startup keeps read-route scope without self-verifying mode."""
    monkeypatch.setenv("XENON_API_TEST_MODE", "0")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU1234567")
    import xenon.api.trading_mode as tm

    importlib.reload(tm)
    import xenon.api.server as server

    importlib.reload(server)
    account, verified = server._resolve_lifespan_account_state("")

    assert account == "DU1234567"
    assert verified is False
