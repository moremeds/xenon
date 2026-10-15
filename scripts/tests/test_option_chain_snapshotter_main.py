"""Tests for the snapshotter entry point — scope + RTH skip behavior.

Mocks IB and Postgres at the module-import boundary so the runtime gates
(RTH check, ticker list, no-env-var exit) are testable without real
infrastructure.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from xenon.option_chain_snapshotter import __main__ as snap_main


def test_tickers_restricted_to_spx_vix() -> None:
    """v1 scope: only SPX and VIX. NDX/RUT are foundation-only, not shipped."""
    assert snap_main.TICKERS == ("SPX", "VIX")


def test_index_exchange_table_matches_tickers() -> None:
    """Every ticker must map to a known exchange; no orphans either way."""
    assert set(snap_main.INDEX_EXCHANGE.keys()) == set(snap_main.TICKERS)
    for sym, exch in snap_main.INDEX_EXCHANGE.items():
        assert exch, f"{sym} has empty exchange"


def test_off_rth_skips_without_db_or_ib(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outside NYSE RTH the entry point must return 0 without ever touching
    the DB or IB. Critical: launchd fires every 600s 24/7 — we must NOT write
    guaranteed-failing rows into archive.snapshot_run overnight or on
    weekends. Mocking is_nyse_rth to False simulates an off-hours fire."""
    monkeypatch.setattr(snap_main, "is_nyse_rth", lambda _ts: False)
    monkeypatch.setattr("sys.argv", ["xenon-option-chain-snapshot"])
    # If the RTH gate fails, the code drops to the OPTION_CHAIN_DATABASE_URL
    # check (which would return 2 in the empty-env case). 0 proves we
    # short-circuited at the RTH gate.
    monkeypatch.delenv("OPTION_CHAIN_DATABASE_URL", raising=False)
    assert asyncio.run(snap_main.run()) == 0


def test_missing_db_url_returns_2_when_forced(monkeypatch: pytest.MonkeyPatch) -> None:
    """--force bypasses RTH; missing DSN should still surface as a config
    error (exit 2) rather than fail late in the IB connect path."""
    monkeypatch.setattr("sys.argv", ["xenon-option-chain-snapshot", "--force"])
    monkeypatch.delenv("OPTION_CHAIN_DATABASE_URL", raising=False)
    assert asyncio.run(snap_main.run()) == 2
