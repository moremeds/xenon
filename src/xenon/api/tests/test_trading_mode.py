"""Unit tests for trading_mode: parse, port mapping, prefix guard.

Each test reloads the module so module-level constants pick up the patched env.
"""

from __future__ import annotations

import importlib
import os

import pytest


def _reload(monkeypatch, mode_value: str | None):
    if mode_value is None:
        monkeypatch.setenv("XENON_TRADING_MODE", "")
    else:
        monkeypatch.setenv("XENON_TRADING_MODE", mode_value)
    import xenon.api.trading_mode as tm

    return importlib.reload(tm)


@pytest.fixture(autouse=True)
def _reset_trading_mode_modules(monkeypatch):
    """Reset XENON_TRADING_MODE / IB_GATEWAY_PORT and reload trading_mode around each test.

    `importlib.reload` is NOT reverted by monkeypatch, so without this fixture
    the trading_mode module would leak whatever the last test's reload left
    its module-level constants set to. Only `xenon.api.trading_mode` is
    reloaded — ib_client must NOT be reloaded mid-session because it would
    invalidate the @patch decorators used by scripts/tests/test_ib_client.py.
    """
    monkeypatch.setenv("XENON_TRADING_MODE", "")
    monkeypatch.delenv("IB_GATEWAY_PORT", raising=False)
    import xenon.api.trading_mode as tm

    importlib.reload(tm)

    yield

    monkeypatch.setenv("XENON_TRADING_MODE", "")
    monkeypatch.delenv("IB_GATEWAY_PORT", raising=False)
    importlib.reload(tm)


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


def test_default_is_paper_when_blank(monkeypatch):
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


@pytest.mark.parametrize(
    "mode,expected_port",
    [("paper", 4002), ("live", 4001)],
)
def test_ib_client_default_port_wired_to_trading_mode(mode, expected_port):
    """Subprocess-isolated wiring check for both modes.

    A simple in-process `assert ibc.DEFAULT_GATEWAY_PORT == tm.EXPECTED_PORT`
    only proves equality under the test session's default mode (paper) —
    a hardcoded `4002` in `ib_client.py` would silently pass. We can't
    `importlib.reload(ibc)` either, since that breaks `@patch` decorators
    in `scripts/tests/test_ib_client.py`. Subprocess gives a fresh interpreter
    that imports `ib_client` from scratch under the patched env, so a
    hardcoded port in either direction fails.
    """
    import subprocess
    import sys

    env = dict(os.environ)
    env["XENON_TRADING_MODE"] = mode
    env.pop("IB_GATEWAY_PORT", None)

    result = subprocess.run(
        [sys.executable, "-c", "from xenon.clients.ib_client import DEFAULT_GATEWAY_PORT; print(DEFAULT_GATEWAY_PORT)"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert int(result.stdout.strip()) == expected_port


def test_ib_gateway_port_env_var_is_ignored(monkeypatch):
    """Spec: IB_GATEWAY_PORT is no longer consulted; mode wins.

    Verifies via the trading_mode module only. The downstream binding to
    `ibc.DEFAULT_GATEWAY_PORT` is covered by
    `test_ib_client_default_port_wired_to_trading_mode`.
    """
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("IB_GATEWAY_PORT", "9999")
    import xenon.api.trading_mode as tm

    importlib.reload(tm)
    assert tm.EXPECTED_PORT == 4002  # mode wins, env var ignored
