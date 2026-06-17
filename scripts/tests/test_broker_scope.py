"""get_broker_scope: IB default, FUTU live, FUTU DB-fallback when OpenD is down."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import xenon.api.guards as guards
from xenon.execution.account_scope import AccountScope


class _FakeRequest:
    def __init__(self):
        self.app = SimpleNamespace(state=SimpleNamespace())


def _patch_futu_client(monkeypatch, client):
    # Patch the real function on the real server module (importing it is fast).
    monkeypatch.setattr("xenon.api.server._get_futu_client", lambda: client, raising=True)


def test_default_broker_resolves_ib_from_app_state(monkeypatch):
    ib = AccountScope(broker="IB", account_env="paper", broker_account="DU1")
    monkeypatch.setattr(guards, "resolve_from_app_state", lambda state: ib)
    assert guards.get_broker_scope(_FakeRequest()) == ib


def test_futu_live_scope_from_connected_client(monkeypatch):
    client = MagicMock()
    client.is_connected.return_value = True
    client.trd_env_of_matched_account.return_value = "REAL"
    client._acc_id = 281753263
    _patch_futu_client(monkeypatch, client)
    scope = guards.get_broker_scope(_FakeRequest(), broker="FUTU")
    assert scope == AccountScope(broker="FUTU", account_env="live", broker_account="281753263")


def test_futu_falls_back_to_db_scope_when_opend_down(monkeypatch):
    from xenon.clients.futu_exceptions import FutuConnectionError

    client = MagicMock()
    client.is_connected.return_value = False
    client.connect.side_effect = FutuConnectionError("OpenD down")
    _patch_futu_client(monkeypatch, client)
    db_scope = AccountScope(broker="FUTU", account_env="live", broker_account="cached-123")
    monkeypatch.setattr(guards, "_futu_scope_from_db", lambda: db_scope)
    assert guards.get_broker_scope(_FakeRequest(), broker="FUTU") == db_scope


def test_futu_503_when_opend_down_and_no_synced_data(monkeypatch):
    from xenon.clients.futu_exceptions import FutuConnectionError

    client = MagicMock()
    client.is_connected.return_value = False
    client.connect.side_effect = FutuConnectionError("OpenD down")
    _patch_futu_client(monkeypatch, client)
    monkeypatch.setattr(guards, "_futu_scope_from_db", lambda: None)
    with pytest.raises(HTTPException) as exc:
        guards.get_broker_scope(_FakeRequest(), broker="FUTU")
    assert exc.value.status_code == 503


def test_performance_scope_alias_points_to_broker_scope():
    assert guards.get_performance_scope is guards.get_broker_scope
