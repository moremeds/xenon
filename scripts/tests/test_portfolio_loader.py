"""Tests for xenon.utils.portfolio_loader.

The autouse `_postgres_orders_test_db` fixture in scripts/tests/conftest.py
truncates xenon.account_snapshots before each test, so we don't need a
separate pg_clean fixture.
"""

from __future__ import annotations

from xenon.utils.portfolio_loader import (
    get_portfolio_tickers_sync,
    load_portfolio_payload_sync,
)

from scripts.tests.helpers.portfolio_seed import seed_portfolio_snapshot
from xenon.execution.account_scope import AccountScope

PAYLOAD_LIVE = {
    "positions": [{"ticker": "AAPL", "structure": "Stock", "legs": []}],
    "bankroll": 100_000,
}
PAYLOAD_PAPER = {
    "positions": [{"ticker": "MSFT", "structure": "Stock", "legs": []}],
    "bankroll": 50_000,
}


def test_returns_none_when_no_snapshots():
    assert load_portfolio_payload_sync() is None


def test_returns_latest_when_scope_none():
    seed_portfolio_snapshot(PAYLOAD_PAPER, account_env="paper", broker_account="DU111")
    seed_portfolio_snapshot(PAYLOAD_LIVE, account_env="live", broker_account="U222")
    payload = load_portfolio_payload_sync()
    assert payload == PAYLOAD_LIVE


def test_filters_by_scope():
    seed_portfolio_snapshot(PAYLOAD_PAPER, account_env="paper", broker_account="DU111")
    seed_portfolio_snapshot(PAYLOAD_LIVE, account_env="live", broker_account="U222")
    paper_scope = AccountScope(broker="IB", account_env="paper", broker_account="DU111")
    live_scope = AccountScope(broker="IB", account_env="live", broker_account="U222")
    assert load_portfolio_payload_sync(scope=paper_scope) == PAYLOAD_PAPER
    assert load_portfolio_payload_sync(scope=live_scope) == PAYLOAD_LIVE


def test_get_portfolio_tickers_sync_extracts_unique_tickers():
    seed_portfolio_snapshot(
        {
            "positions": [
                {"ticker": "AAPL", "legs": []},
                {"ticker": "aapl", "legs": []},
                {"ticker": "MSFT", "legs": []},
            ],
        }
    )
    assert get_portfolio_tickers_sync() == ["AAPL", "MSFT"]


def test_get_portfolio_tickers_sync_returns_empty_when_no_snapshot():
    assert get_portfolio_tickers_sync() == []
