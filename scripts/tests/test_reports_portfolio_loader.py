"""Regression: report modules read portfolio from Postgres (Phase-2 migration)."""

from __future__ import annotations

import pytest

# Phase 2 carve-out: report modules open their own SQLAlchemy engine, so the
# test's BEGIN/ROLLBACK can't cover them. Stays on Phase 1 TRUNCATE pre+post.
pytestmark = pytest.mark.committed_db

from scripts.tests.helpers.portfolio_seed import seed_portfolio_snapshot

PAYLOAD = {
    "positions": [
        {
            "ticker": "AAPL",
            "structure": "Stock",
            "expiry": "N/A",
            "contracts": 100,
            "market_price": 190,
            "avg_cost": 185,
            "legs": [
                {
                    "direction": "LONG",
                    "type": "Stock",
                    "contracts": 100,
                    "strike": 0,
                    "avg_cost": 185,
                    "market_price": 190,
                }
            ],
        }
    ],
    "bankroll": 100_000,
    "account_summary": {"net_liquidation": 100_000},
}


def test_portfolio_performance_load_snapshot_reads_pg():
    from xenon.reports.portfolio_performance import load_portfolio_snapshot

    seed_portfolio_snapshot(PAYLOAD)
    snapshot = load_portfolio_snapshot()
    assert snapshot["positions"][0]["ticker"] == "AAPL"


def test_portfolio_performance_load_snapshot_empty_when_no_snapshot():
    from xenon.reports.portfolio_performance import load_portfolio_snapshot

    assert load_portfolio_snapshot() == {}
