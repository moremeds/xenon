"""Tests for GET /performance route + Futu sync NAV-persistence wiring.

Pattern follows test_server_futu_routes.py — boot the FastAPI app in test
mode so lifespan skips IB pool startup; mock the cache/service/Futu
singleton for route-plumbing tests.
"""
from __future__ import annotations

import os
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ["XENON_API_TEST_MODE"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from xenon.api import server  # noqa: E402
from xenon.api.services.futu_nav_persistence import NavAccountEnvConflict  # noqa: E402
from xenon.execution.account_scope import AccountScope  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    """TestClient with a clean DATA_DIR + reset Futu singleton."""
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "_futu_last_sync_monotonic", None, raising=False)
    monkeypatch.setattr(server, "_futu_client", None, raising=False)
    monkeypatch.setattr(server, "_futu_lock", None, raising=False)
    # Provide a sentinel db_engine so the route's None-check passes — we
    # patch cached_compute below, so the engine value is never used.
    server.app.state.db_engine = object()
    return TestClient(server.app)


# ──────────────────────────────────────────────────────────────────────
# Route plumbing — IB default
# ──────────────────────────────────────────────────────────────────────


def test_performance_ib_default_returns_payload(client: TestClient) -> None:
    fake_payload = {
        "status": "ok",
        "summary": {"sharpe_ratio": None},
        "series": [],
        "warnings": [],
        "scope": {"broker": "IB", "account_env": "paper", "broker_account": "DU0000000"},
    }
    with patch(
        "xenon.api.routes.performance.cached_compute",
        new=AsyncMock(return_value=fake_payload),
    ):
        resp = client.get("/performance")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["scope"]["broker"] == "IB"


def test_performance_scope_passes_ib_account_scope_to_service(client: TestClient) -> None:
    captured: dict[str, AccountScope] = {}

    async def _fake(engine, scope, *, ib_pool=None):
        captured["scope"] = scope
        return {"status": "ok", "summary": {}, "series": [], "warnings": [], "scope": scope.as_dict()}

    with patch("xenon.api.routes.performance.cached_compute", new=_fake):
        resp = client.get("/performance")
    assert resp.status_code == 200
    assert captured["scope"].broker == "IB"
    assert captured["scope"].account_env == "paper"
    assert captured["scope"].broker_account == "DU0000000"


def test_performance_missing_db_engine_returns_503(client: TestClient) -> None:
    server.app.state.db_engine = None
    resp = client.get("/performance")
    assert resp.status_code == 503
    assert "db engine" in resp.json()["detail"].lower()


def test_performance_unknown_broker_returns_400(client: TestClient) -> None:
    resp = client.get("/performance?broker=ROBINHOOD")
    assert resp.status_code == 400


def test_performance_ib_falls_back_to_env_when_app_state_account_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression — surfaced by live test 2026-06-01.

    When IB Gateway TCP is open but the API handshake times out (e.g. client
    ID conflict, 2FA pending), the lifespan leaves app.state.account="".
    The perf-rebuild read path is Postgres-only, so the route must still
    serve via the XENON_BROKER_ACCOUNT + XENON_TRADING_MODE env fallback
    instead of crashing with ValueError → 500.
    """
    # Force the empty-state condition the live failure produced.
    server.app.state.account = ""
    # The autouse conftest fixture set XENON_TRADING_MODE=paper and module-loaded
    # xenon.api.trading_mode with MODE="paper". For this regression we use a paper
    # account ID (DU…) so resolve_from_env() validates cleanly without reloading
    # the trading_mode module — the bug we're locking in is "env fallback fires
    # instead of crashing on empty app.state.account", which is broker/env agnostic.
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU9999999")

    captured: dict[str, AccountScope] = {}

    async def _fake(engine, scope, *, ib_pool=None):
        captured["scope"] = scope
        return {"status": "ok", "summary": {}, "series": [], "warnings": [], "scope": scope.as_dict()}

    with patch("xenon.api.routes.performance.cached_compute", new=_fake):
        resp = client.get("/performance?broker=IB")

    # Restore so other tests don't see the empty account.
    server.app.state.account = "DU0000000"

    assert resp.status_code == 200, resp.text
    assert captured["scope"].broker == "IB"
    assert captured["scope"].account_env == "paper"
    assert captured["scope"].broker_account == "DU9999999"


def test_performance_nav_conflict_returns_409(client: TestClient) -> None:
    scope = AccountScope("FUTU", "live", "12345")
    conflict = NavAccountEnvConflict(scope, "paper", date(2026, 6, 1))
    with patch(
        "xenon.api.routes.performance.cached_compute",
        new=AsyncMock(side_effect=conflict),
    ):
        resp = client.get("/performance")
    assert resp.status_code == 409
    assert "conflict" in resp.json()["detail"].lower()


# ──────────────────────────────────────────────────────────────────────
# FUTU broker path — Futu OpenD up / down
# ──────────────────────────────────────────────────────────────────────


def test_performance_futu_when_opend_unreachable_returns_503(client: TestClient) -> None:
    from xenon.clients.futu_exceptions import FutuConnectionError

    fake_client = MagicMock()
    fake_client.is_connected.return_value = False
    fake_client.connect.side_effect = FutuConnectionError("no opend")
    with patch("xenon.api.server._get_futu_client", return_value=fake_client):
        resp = client.get("/performance?broker=FUTU")
    assert resp.status_code == 503
    assert "opend unreachable" in resp.json()["detail"].lower()


def test_performance_futu_resolves_scope_from_matched_account(client: TestClient) -> None:
    fake_client = MagicMock()
    fake_client.is_connected.return_value = True
    fake_client.trd_env_of_matched_account.return_value = "REAL"
    fake_client._acc_id = 999777

    captured: dict[str, AccountScope] = {}

    async def _fake(engine, scope, *, ib_pool=None):
        captured["scope"] = scope
        return {"status": "ok", "summary": {}, "series": [], "warnings": [], "scope": scope.as_dict()}

    with patch("xenon.api.server._get_futu_client", return_value=fake_client), \
         patch("xenon.api.routes.performance.cached_compute", new=_fake):
        resp = client.get("/performance?broker=FUTU")
    assert resp.status_code == 200, resp.text
    assert captured["scope"].broker == "FUTU"
    assert captured["scope"].account_env == "live"  # REAL → live (correction #18)
    assert captured["scope"].broker_account == "999777"


def test_performance_futu_simulate_maps_to_paper(client: TestClient) -> None:
    fake_client = MagicMock()
    fake_client.is_connected.return_value = True
    fake_client.trd_env_of_matched_account.return_value = "SIMULATE"
    fake_client._acc_id = 111222

    captured: dict[str, AccountScope] = {}

    async def _fake(engine, scope, *, ib_pool=None):
        captured["scope"] = scope
        return {"status": "ok", "summary": {}, "series": [], "warnings": [], "scope": scope.as_dict()}

    with patch("xenon.api.server._get_futu_client", return_value=fake_client), \
         patch("xenon.api.routes.performance.cached_compute", new=_fake):
        resp = client.get("/performance?broker=FUTU")
    assert resp.status_code == 200
    # Correction #18: SIMULATE → "paper" (aligned with IB convention)
    assert captured["scope"].account_env == "paper"


# ──────────────────────────────────────────────────────────────────────
# POST /futu/sync — NAV persistence wiring
# ──────────────────────────────────────────────────────────────────────


def test_futu_sync_calls_persist_futu_nav(client: TestClient) -> None:
    """A successful Futu sync should attempt to persist the NAV row."""
    fake_client = MagicMock()
    fake_client.is_connected.return_value = True
    fake_client.trd_env_of_matched_account.return_value = "REAL"
    fake_payload = {
        "fetched_at": "2026-06-01T12:00:00Z",
        "data_as_of": "2026-06-01T12:00:00Z",
        "account_id": "12345",
        "source": "futu",
        "positions": [],
        "count": 0,
        "account_summary": {"net_liquidation": 100000.0},
        "warnings": [],
    }
    fake_client.fetch_portfolio.return_value = fake_payload

    persist_mock = AsyncMock()
    with patch("xenon.api.server._get_futu_client", return_value=fake_client), \
         patch("xenon.api.services.futu_nav_persistence.persist_futu_nav", new=persist_mock):
        resp = client.post("/futu/sync")
    assert resp.status_code == 200
    persist_mock.assert_called_once()


def test_futu_sync_persist_conflict_returns_409(client: TestClient) -> None:
    fake_client = MagicMock()
    fake_client.is_connected.return_value = True
    fake_client.trd_env_of_matched_account.return_value = "REAL"
    fake_client.fetch_portfolio.return_value = {
        "fetched_at": "2026-06-01T12:00:00Z",
        "data_as_of": "2026-06-01T12:00:00Z",
        "account_id": "12345",
        "source": "futu",
        "positions": [],
        "count": 0,
        "account_summary": {"net_liquidation": 100000.0},
        "warnings": [],
    }
    scope = AccountScope("FUTU", "live", "12345")
    conflict = NavAccountEnvConflict(scope, "paper", date(2026, 6, 1))
    with patch("xenon.api.server._get_futu_client", return_value=fake_client), \
         patch(
             "xenon.api.services.futu_nav_persistence.persist_futu_nav",
             new=AsyncMock(side_effect=conflict),
         ):
        resp = client.post("/futu/sync")
    assert resp.status_code == 409


def test_futu_sync_persist_other_failure_still_returns_200(client: TestClient) -> None:
    """Non-conflict persistence failures must not mask a successful OpenD fetch."""
    fake_client = MagicMock()
    fake_client.is_connected.return_value = True
    fake_client.trd_env_of_matched_account.return_value = "REAL"
    fake_client.fetch_portfolio.return_value = {
        "fetched_at": "2026-06-01T12:00:00Z",
        "data_as_of": "2026-06-01T12:00:00Z",
        "account_id": "12345",
        "source": "futu",
        "positions": [],
        "count": 0,
        "account_summary": {"net_liquidation": 100000.0},
        "warnings": [],
    }
    with patch("xenon.api.server._get_futu_client", return_value=fake_client), \
         patch(
             "xenon.api.services.futu_nav_persistence.persist_futu_nav",
             new=AsyncMock(side_effect=RuntimeError("transient db hiccup")),
         ):
        resp = client.post("/futu/sync")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
