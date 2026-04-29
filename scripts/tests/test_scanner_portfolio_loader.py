"""Regression: scanner.get_open_positions and leap_iv.load_portfolio_tickers
read from Postgres, not data/portfolio.json (Phase-2 migration).
"""

from __future__ import annotations

from scripts.tests.helpers.portfolio_seed import seed_portfolio_snapshot


def test_scanner_get_open_positions_reads_postgres():
    from xenon.scanners.scanner import get_open_positions

    seed_portfolio_snapshot(
        {
            "positions": [
                {"ticker": "AAPL", "legs": []},
                {"ticker": "MSFT", "legs": []},
            ],
        }
    )
    assert get_open_positions() == {"AAPL", "MSFT"}


def test_scanner_get_open_positions_empty_when_no_snapshot():
    from xenon.scanners.scanner import get_open_positions

    assert get_open_positions() == set()


def test_leap_iv_load_portfolio_tickers_reads_postgres():
    from xenon.scanners.leap_iv import load_portfolio_tickers

    seed_portfolio_snapshot(
        {
            "positions": [
                {"ticker": "AAPL", "structure_type": "Long Call", "legs": []},
                {"ticker": "TSLA", "structure_type": "Risk Reversal", "legs": []},
            ],
        }
    )
    result = load_portfolio_tickers()
    assert result == {"AAPL": "Long Call", "TSLA": "Risk Reversal"}


def test_leap_iv_load_portfolio_tickers_empty_when_no_snapshot():
    from xenon.scanners.leap_iv import load_portfolio_tickers

    assert load_portfolio_tickers() == {}
