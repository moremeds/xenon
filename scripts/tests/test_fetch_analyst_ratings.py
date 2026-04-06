"""Tests for fetch_analyst_ratings.py — signal calculation and data extraction."""
import json
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

from fetch_analyst_ratings import (
    calculate_rating_signal,
    get_watchlist_tickers,
    get_portfolio_tickers,
    get_cached_rating,
    format_ratings_table,
    CACHE_TTL_HOURS,
)


# ── calculate_rating_signal ─────────────────────────────────────────

class TestCalculateRatingSignal:
    def test_high_buy_pct_bullish(self):
        data = {
            "ratings": {"buy_pct": 80, "sell_pct": 5, "total": 25},
        }
        signal = calculate_rating_signal(data)
        assert signal["direction"] == "BULLISH"
        assert signal["strength"] >= 80

    def test_moderate_buy_lean_bullish(self):
        data = {
            "ratings": {"buy_pct": 55, "sell_pct": 10, "total": 15},
        }
        signal = calculate_rating_signal(data)
        assert signal["direction"] == "LEAN_BULLISH"

    def test_high_sell_pct_lean_bearish(self):
        data = {
            "ratings": {"buy_pct": 20, "sell_pct": 55, "total": 20},
        }
        signal = calculate_rating_signal(data)
        assert signal["direction"] == "LEAN_BEARISH"

    def test_neutral_direction(self):
        data = {
            "ratings": {"buy_pct": 30, "sell_pct": 30, "total": 10},
        }
        signal = calculate_rating_signal(data)
        assert signal["direction"] == "NEUTRAL"

    def test_high_analyst_count_high_confidence(self):
        data = {
            "ratings": {"buy_pct": 70, "sell_pct": 10, "total": 25},
        }
        signal = calculate_rating_signal(data)
        assert signal["confidence"] == "HIGH"

    def test_medium_analyst_count(self):
        data = {
            "ratings": {"buy_pct": 70, "sell_pct": 10, "total": 15},
        }
        signal = calculate_rating_signal(data)
        assert signal["confidence"] == "MEDIUM"

    def test_low_analyst_count_low_confidence(self):
        data = {
            "ratings": {"buy_pct": 70, "sell_pct": 10, "total": 5},
        }
        signal = calculate_rating_signal(data)
        assert signal["confidence"] == "LOW"

    def test_no_ratings_data(self):
        signal = calculate_rating_signal({"error": "no data"})
        assert signal["direction"] == "NEUTRAL"
        assert signal["confidence"] == "LOW"

    def test_zero_total_analysts(self):
        data = {"ratings": {"buy_pct": 0, "sell_pct": 0, "total": 0}}
        signal = calculate_rating_signal(data)
        assert signal["direction"] == "NEUTRAL"

    def test_upgrading_changes_signal(self):
        data = {
            "ratings": {"buy_pct": 70, "sell_pct": 10, "total": 20},
            "has_recent_changes": True,
            "recent_changes": [
                {"category": "buy", "change": 3},
                {"category": "sell", "change": 0},
            ],
        }
        signal = calculate_rating_signal(data)
        assert signal["changes_signal"] == "UPGRADING"

    def test_target_upside_note(self):
        data = {
            "ratings": {"buy_pct": 70, "sell_pct": 10, "total": 20},
            "target_upside_pct": 25.0,
        }
        signal = calculate_rating_signal(data)
        assert any("Bullish" in n for n in signal["notes"])


# ── get_watchlist_tickers ───────────────────────────────────────────

class TestGetWatchlistTickers:
    def test_extracts_tickers(self, tmp_path):
        wl = tmp_path / "watchlist.json"
        wl.write_text(json.dumps({
            "tickers": [
                {"ticker": "AAPL"},
                {"ticker": "MSFT"},
            ],
            "subcategories": {
                "@someone": {"tickers": [{"ticker": "NVDA"}]}
            }
        }))
        with patch("fetch_analyst_ratings.WATCHLIST_FILE", wl):
            tickers = get_watchlist_tickers()
            assert "AAPL" in tickers
            assert "MSFT" in tickers
            assert "NVDA" in tickers

    def test_empty_watchlist(self, tmp_path):
        wl = tmp_path / "watchlist.json"
        wl.write_text(json.dumps({}))
        with patch("fetch_analyst_ratings.WATCHLIST_FILE", wl):
            assert get_watchlist_tickers() == []


# ── get_portfolio_tickers ───────────────────────────────────────────

class TestGetPortfolioTickers:
    def test_extracts_tickers(self, tmp_path):
        pf = tmp_path / "portfolio.json"
        pf.write_text(json.dumps({
            "positions": [
                {"ticker": "AAPL"},
                {"ticker": "NVDA"},
            ]
        }))
        with patch("fetch_analyst_ratings.PORTFOLIO_FILE", pf):
            tickers = get_portfolio_tickers()
            assert "AAPL" in tickers
            assert "NVDA" in tickers


# ── get_cached_rating ───────────────────────────────────────────────

class TestCachedRating:
    def test_fresh_cache_returned(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        fresh_time = datetime.now().isoformat()
        cache_file.write_text(json.dumps({
            "ratings": {
                "AAPL": {"fetched_at": fresh_time, "recommendation": "buy"}
            }
        }))
        with patch("fetch_analyst_ratings.RATINGS_CACHE_FILE", cache_file):
            result = get_cached_rating("AAPL")
            assert result is not None
            assert result["from_cache"] is True

    def test_stale_cache_returns_none(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        stale_time = (datetime.now() - timedelta(hours=CACHE_TTL_HOURS + 1)).isoformat()
        cache_file.write_text(json.dumps({
            "ratings": {
                "AAPL": {"fetched_at": stale_time, "recommendation": "buy"}
            }
        }))
        with patch("fetch_analyst_ratings.RATINGS_CACHE_FILE", cache_file):
            result = get_cached_rating("AAPL")
            assert result is None

    def test_missing_ticker_returns_none(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps({"ratings": {}}))
        with patch("fetch_analyst_ratings.RATINGS_CACHE_FILE", cache_file):
            assert get_cached_rating("FAKE") is None


# ── format_ratings_table ────────────────────────────────────────────

class TestFormatRatingsTable:
    def test_empty_results(self):
        output = format_ratings_table([])
        # Empty list with changes_only=False still returns header
        assert "ANALYST RATINGS" in output or "No analyst rating changes" in output

    def test_changes_only_filter(self):
        results = [
            {"ticker": "AAPL", "ratings": {"buy_pct": 70, "sell_pct": 5, "total": 20,
             "hold": 5}, "recommendation": "buy", "target_price": {"mean": 200},
             "has_recent_changes": False},
            {"ticker": "NVDA", "ratings": {"buy_pct": 80, "sell_pct": 5, "total": 30,
             "hold": 5}, "recommendation": "buy", "target_price": {"mean": 500},
             "has_recent_changes": True, "recent_changes": [
                 {"category": "buy", "previous": 25, "current": 28, "change": 3}
             ]},
        ]
        output = format_ratings_table(results, changes_only=True)
        assert "NVDA" in output

    def test_no_changes_message(self):
        output = format_ratings_table([], changes_only=True)
        assert "No analyst rating changes" in output
