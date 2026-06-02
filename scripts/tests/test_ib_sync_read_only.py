"""ib_sync's PG-write helpers no-op when XENON_READ_ONLY=1.

Pairs with the FastAPI read-only guard. dev.sh live exports the flag so
that a debugging session against live IB cannot drift core_test by
persisting positions/NAV pulled from prod IBKR.
"""

from __future__ import annotations

import pytest

from xenon.execution import ib_sync


@pytest.fixture
def read_only_env(monkeypatch):
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    yield


@pytest.fixture
def writable_env(monkeypatch):
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    yield


def test_save_portfolio_noop_in_read_only(read_only_env, monkeypatch, capsys):
    """Read-only mode short-circuits before touching get_sync_engine.

    We replace get_sync_engine with a raiser; the function must return
    without calling it. The skip notice goes to stdout so the operator
    sees it during dev.sh live.
    """

    def _raiser(*args, **kwargs):
        raise AssertionError("get_sync_engine() called in read-only mode — guard regression.")

    monkeypatch.setattr(ib_sync, "get_sync_engine", _raiser)

    # Three positions so the message can report a non-zero count.
    portfolio = {"positions": [{"symbol": "SPY"}, {"symbol": "QQQ"}, {"symbol": "IWM"}]}
    result = ib_sync._save_portfolio_to_postgres(portfolio)

    assert result is None
    captured = capsys.readouterr()
    assert "XENON_READ_ONLY" in captured.out
    assert "3 positions" in captured.out


def test_append_nav_snapshot_noop_in_read_only(read_only_env, monkeypatch, capsys):
    """Same shape for NAV — no engine call, no DB row."""

    def _raiser(*args, **kwargs):
        raise AssertionError("get_sync_engine() called in read-only mode — guard regression.")

    monkeypatch.setattr(ib_sync, "get_sync_engine", _raiser)

    result = ib_sync._append_nav_snapshot(12345.67, daily_pnl=100.0)

    assert result is None
    captured = capsys.readouterr()
    assert "XENON_READ_ONLY" in captured.out
    assert "12,345.67" in captured.out


def test_save_portfolio_calls_engine_when_writable(writable_env, monkeypatch):
    """Sanity: the no-op is gated on the flag, not unconditional.

    We replace get_sync_engine with a sentinel-raiser; in the writable
    branch the function MUST reach it (then the raiser fires and we
    catch the marker). Either:
      - the function gets past the read-only short-circuit AND tries to
        write (RuntimeError marker) → pass
      - it short-circuits anyway (no exception) → fail
    """
    marker = RuntimeError("engine-was-called")

    def _engine_called(*args, **kwargs):
        raise marker

    monkeypatch.setattr(ib_sync, "get_sync_engine", _engine_called)

    portfolio = {"positions": []}
    with pytest.raises(RuntimeError, match="engine-was-called"):
        ib_sync._save_portfolio_to_postgres(portfolio)
