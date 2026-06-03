"""xenon-nav-flex-refresh CLI behavior (PR-1)."""

from __future__ import annotations

import importlib
import os
from unittest.mock import patch


def _reimport_module():
    """Force re-import so module-level state (env reads) refreshes per test."""
    import xenon.jobs.nav_flex_refresh as m

    importlib.reload(m)
    return m


def test_main_exits_2_when_token_missing(monkeypatch, capsys):
    # Set to empty string instead of delenv: _load_env() runs after monkeypatch
    # and pulls from .env (load_dotenv default override=False, so existing
    # empty value stays). 'if not token' still trips on the empty string.
    monkeypatch.setenv("IB_FLEX_TOKEN", "")
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "")
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    m = _reimport_module()
    rc = m.main()
    assert rc == 2
    err = capsys.readouterr().err
    assert "FLEX_NOT_CONFIGURED" in err


def test_main_exits_3_when_read_only(monkeypatch, capsys):
    """Pass-1: refuse to write when XENON_READ_ONLY=1. A MacBook `dev.sh live`
    session would otherwise pollute core_test with live close NAVs."""
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    # Even with valid token + query — the read-only flag short-circuits first.
    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1234567")
    m = _reimport_module()
    rc = m.main()
    assert rc == 3
    err = capsys.readouterr().err
    assert "READ_ONLY" in err
    assert "XENON_READ_ONLY=1" in err


def test_main_exits_1_when_fetch_returns_none(monkeypatch):
    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1234567")
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_LIVE_ACCOUNT", "U18007831")
    m = _reimport_module()
    with patch.object(m, "fetch_ib_nav_series", return_value=None):
        rc = m.main()
    assert rc == 1


def test_main_exits_1_when_fetch_returns_empty(monkeypatch):
    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1234567")
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "U18007831")
    m = _reimport_module()
    with patch.object(m, "fetch_ib_nav_series", return_value=[]):
        rc = m.main()
    assert rc == 1


def test_main_derives_broker_account_from_live(monkeypatch, capsys):
    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1234567")
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_LIVE_ACCOUNT", "U18007831")
    monkeypatch.delenv("XENON_BROKER_ACCOUNT", raising=False)
    m = _reimport_module()
    sample = [{"date": "2026-06-01", "total": 100.0, "cash": 50.0, "stock": 40.0, "options": 10.0}]
    with patch.object(m, "fetch_ib_nav_series", return_value=sample):
        rc = m.main()
    assert rc == 0
    assert os.environ["XENON_BROKER_ACCOUNT"] == "U18007831"
    out = capsys.readouterr().out
    assert "fetched 1 NAV row" in out


def test_main_derives_broker_account_from_paper(monkeypatch):
    monkeypatch.setenv("IB_FLEX_TOKEN", "x" * 24)
    monkeypatch.setenv("IB_FLEX_NAV_QUERY_ID", "1234567")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_PAPER_ACCOUNT", "DUQ378889")
    monkeypatch.delenv("XENON_BROKER_ACCOUNT", raising=False)
    m = _reimport_module()
    with patch.object(m, "fetch_ib_nav_series", return_value=[{"date": "2026-06-01"}]):
        rc = m.main()
    assert rc == 0
    assert os.environ["XENON_BROKER_ACCOUNT"] == "DUQ378889"
