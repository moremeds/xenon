"""Unit tests for AccountScope resolver."""

from __future__ import annotations

import pytest


def test_scope_is_frozen():
    from xenon.execution.account_scope import AccountScope

    scope = AccountScope(broker="IB", account_env="paper", broker_account="DU1234567")
    with pytest.raises(AttributeError):
        scope.broker = "FUTU"  # type: ignore[misc]


def test_scope_dict():
    from xenon.execution.account_scope import AccountScope

    scope = AccountScope(broker="IB", account_env="paper", broker_account="DU1234567")
    assert scope.as_dict() == {
        "broker": "IB",
        "account_env": "paper",
        "broker_account": "DU1234567",
    }


def _reload_trading_mode_with(monkeypatch, mode: str):
    import importlib

    monkeypatch.setenv("XENON_TRADING_MODE", mode)
    import xenon.api.trading_mode as tm

    return importlib.reload(tm)


def test_resolve_from_env_paper(monkeypatch):
    _reload_trading_mode_with(monkeypatch, "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU9999999")
    from xenon.execution.account_scope import resolve_from_env

    scope = resolve_from_env()
    assert scope.broker == "IB"
    assert scope.account_env == "paper"
    assert scope.broker_account == "DU9999999"


def test_resolve_from_env_live(monkeypatch):
    _reload_trading_mode_with(monkeypatch, "live")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "U1234567")
    from xenon.execution.account_scope import resolve_from_env

    scope = resolve_from_env()
    assert scope.broker == "IB"
    assert scope.account_env == "live"
    assert scope.broker_account == "U1234567"


def test_resolve_from_app_state_preserves_broker():
    from types import SimpleNamespace

    from xenon.execution.account_scope import resolve_from_app_state

    scope = resolve_from_app_state(
        SimpleNamespace(broker="FUTU", trading_mode="live", account="FUTU-US")
    )
    assert scope.broker == "FUTU"
    assert scope.account_env == "live"
    assert scope.broker_account == "FUTU-US"


def test_resolve_from_env_missing_account_raises(monkeypatch):
    _reload_trading_mode_with(monkeypatch, "paper")
    monkeypatch.delenv("XENON_BROKER_ACCOUNT", raising=False)
    from xenon.execution.account_scope import resolve_from_env

    with pytest.raises(ValueError, match="XENON_BROKER_ACCOUNT"):
        resolve_from_env()


def test_resolve_from_env_mismatch_raises(monkeypatch):
    _reload_trading_mode_with(monkeypatch, "live")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU1234567")
    from xenon.execution.account_scope import resolve_from_env

    with pytest.raises(ValueError, match="mismatch"):
        resolve_from_env()
