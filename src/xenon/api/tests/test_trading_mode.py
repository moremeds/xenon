"""Unit tests for trading_mode: parse, port mapping, prefix guard.

Each test reloads the module so module-level constants pick up the patched env.
"""

from __future__ import annotations

import importlib

import pytest


def _reload(monkeypatch, mode_value: str | None):
    if mode_value is None:
        monkeypatch.delenv("XENON_TRADING_MODE", raising=False)
    else:
        monkeypatch.setenv("XENON_TRADING_MODE", mode_value)
    import xenon.api.trading_mode as tm

    return importlib.reload(tm)


def test_parse_paper(monkeypatch):
    tm = _reload(monkeypatch, "paper")
    assert tm.MODE == "paper"
    assert tm.EXPECTED_PORT == 4002
    assert tm.EXPECTED_PREFIX == "DU"


def test_parse_live(monkeypatch):
    tm = _reload(monkeypatch, "live")
    assert tm.MODE == "live"
    assert tm.EXPECTED_PORT == 4001
    assert tm.EXPECTED_PREFIX == "U"


def test_default_is_paper_when_unset(monkeypatch):
    tm = _reload(monkeypatch, None)
    assert tm.MODE == "paper"
    assert tm.EXPECTED_PORT == 4002


def test_case_insensitive_and_trimmed(monkeypatch):
    tm = _reload(monkeypatch, "  LIVE  ")
    assert tm.MODE == "live"


def test_invalid_value_raises_at_import(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", "demo")
    import xenon.api.trading_mode as tm

    with pytest.raises(ValueError, match="XENON_TRADING_MODE"):
        importlib.reload(tm)


def test_verify_account_paper_matches(monkeypatch):
    tm = _reload(monkeypatch, "paper")
    assert tm.verify_account("DU1234567") is True


def test_verify_account_live_matches(monkeypatch):
    tm = _reload(monkeypatch, "live")
    assert tm.verify_account("U1234567") is True


def test_verify_account_paper_rejects_live(monkeypatch):
    tm = _reload(monkeypatch, "paper")
    assert tm.verify_account("U1234567") is False


def test_verify_account_live_rejects_paper(monkeypatch):
    tm = _reload(monkeypatch, "live")
    # "DU…" must NOT match live's "U" prefix — the live check must reject DU explicitly
    assert tm.verify_account("DU1234567") is False


def test_verify_account_empty_is_false(monkeypatch):
    tm = _reload(monkeypatch, "live")
    assert tm.verify_account("") is False
    assert tm.verify_account(None) is False  # type: ignore[arg-type]


def test_default_gateway_port_follows_mode_paper(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.delenv("IB_GATEWAY_PORT", raising=False)
    import xenon.api.trading_mode as tm

    importlib.reload(tm)
    import xenon.clients.ib_client as ibc

    importlib.reload(ibc)
    assert ibc.DEFAULT_GATEWAY_PORT == 4002


def test_default_gateway_port_follows_mode_live(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.delenv("IB_GATEWAY_PORT", raising=False)
    import xenon.api.trading_mode as tm

    importlib.reload(tm)
    import xenon.clients.ib_client as ibc

    importlib.reload(ibc)
    assert ibc.DEFAULT_GATEWAY_PORT == 4001


def test_ib_gateway_port_env_var_is_ignored(monkeypatch):
    """Spec: IB_GATEWAY_PORT is no longer consulted; mode wins."""
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("IB_GATEWAY_PORT", "9999")
    import xenon.api.trading_mode as tm

    importlib.reload(tm)
    import xenon.clients.ib_client as ibc

    importlib.reload(ibc)
    assert ibc.DEFAULT_GATEWAY_PORT == 4002  # mode wins, env var ignored
