"""Risk guard api wiring: BLIND system causes, start gate, routes, /health block."""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from xenon.api import server
from xenon.api.routes import risk as risk_routes
from xenon.api.routes import system as system_routes
from xenon.api.services.risk_guard import app as rg_app

NOW = datetime(2026, 11, 9, 15, 0, tzinfo=timezone.utc)


class Pool:
    def __init__(self, connected=True, upstream="connected"):
        self.connected, self.upstream = connected, upstream

    def is_connected(self, role):
        return self.connected

    def upstream_status(self):
        return {"state": self.upstream}


class Owner:
    def __init__(self, halted):
        self.halted = halted

    def status(self):
        return {"halt_reason": "clientId in use"}


@pytest.fixture
def state(monkeypatch):
    st = SimpleNamespace(trading_mode="live", account="U1", mode_verified=True, broker="IB")
    monkeypatch.setattr(server, "app", SimpleNamespace(state=st))
    monkeypatch.setattr(server, "ib_pool", Pool())
    monkeypatch.setattr(server, "order_owner", Owner(False))
    return st


def test_healthy_process_has_no_system_cause(state):
    assert rg_app.system_health(NOW) == {}


def test_halted_owner_down_data_role_and_unverified_scope(state, monkeypatch):
    monkeypatch.setattr(server, "order_owner", Owner(True))
    monkeypatch.setattr(server, "ib_pool", Pool(connected=False))
    state.mode_verified = False
    causes = rg_app.system_health(NOW)
    assert set(causes) == {"OWNER_HALTED", "DATA_ROLE_DOWN", "SCOPE_UNRESOLVED"}


def test_upstream_lost(state, monkeypatch):
    monkeypatch.setattr(server, "ib_pool", Pool(upstream="lost"))
    assert set(rg_app.system_health(NOW)) == {"UPSTREAM_LOST"}


def test_scope_falls_back_to_env(state, monkeypatch):
    state.account = None
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU7")
    monkeypatch.setattr("xenon.api.trading_mode.MODE", "paper")
    scope, verified = rg_app.resolve_scope(state)
    assert scope.broker_account == "DU7" and verified is False


def test_not_started_in_test_mode_or_without_env(state, monkeypatch):
    monkeypatch.setattr(server, "_is_test_mode", lambda: True)
    monkeypatch.setenv("XENON_RISK_GUARD", "1")
    rg_app.maybe_start()
    assert state.risk_guard is None and state.risk_guard_task is None
    monkeypatch.setattr(server, "_is_test_mode", lambda: False)
    monkeypatch.delenv("XENON_RISK_GUARD")
    rg_app.maybe_start()
    assert state.risk_guard_task is None


class FakeGuard:
    def __init__(self, ack="ok"):
        self._ack, self.health, self.last_tick_at = ack, {"FEED_STALE": "x"}, "t"
        self.enabled_fn = lambda: True

    async def ack(self, key):
        return self._ack

    def state(self):
        return {"enabled": True, "tier": "FAST", "account": {"nlv": 1.0}}


@pytest.mark.parametrize(
    ("ack", "status", "code"),
    [("unknown", 404, "RISK_INCIDENT_UNKNOWN"), ("unavailable", 503, "RISK_ACK_UNAVAILABLE")],
)
def test_ack_refusals_have_top_level_reason_code(state, ack, status, code):
    state.risk_guard = FakeGuard(ack)
    resp = asyncio.run(risk_routes.risk_ack("market"))
    assert resp.status_code == status and json.loads(resp.body)["reason_code"] == code


def test_ack_ok_and_guard_off(state):
    state.risk_guard = FakeGuard()
    assert asyncio.run(risk_routes.risk_ack("market")) == {"key": "market", "acked": True}
    state.risk_guard = None
    assert asyncio.run(risk_routes.risk_ack("market")).status_code == 409


def test_ack_refused_read_only(state, monkeypatch):
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    state.risk_guard = FakeGuard()
    resp = asyncio.run(risk_routes.risk_ack("market"))
    assert resp.status_code == 403 and json.loads(resp.body)["reason_code"] == "READ_ONLY_MODE"


def test_health_block_has_no_account_values(state):
    state.risk_guard = FakeGuard()
    block = system_routes._risk_guard_health()
    assert block == {"running": True, "enabled": True, "tier": "FAST", "blind": ["FEED_STALE"], "last_tick_at": "t"}
    state.risk_guard = None
    assert system_routes._risk_guard_health() == {"running": False}


def test_risk_paths_not_open_to_api_keys():
    from xenon.api.auth import API_KEY_ALLOWED_PATHS, QUERY_API_KEY_PATHS

    assert not any(p.startswith("/risk") for p in API_KEY_ALLOWED_PATHS)
    assert not any(p.startswith("/risk") for _m, p in QUERY_API_KEY_PATHS)
