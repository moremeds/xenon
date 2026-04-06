"""Tests for fetch_flow.py — dark pool + options flow analysis."""
import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock

from fetch_flow import (
    is_market_open,
    get_last_n_trading_days,
    analyze_darkpool,
    analyze_options_flow,
    MARKET_HOLIDAYS_2026,
)


# ── is_market_open ──────────────────────────────────────────────────

class TestIsMarketOpen:
    def test_weekday_open(self):
        # Tuesday 2026-03-03
        assert is_market_open(datetime(2026, 3, 3)) is True

    def test_saturday_closed(self):
        assert is_market_open(datetime(2026, 3, 7)) is False

    def test_sunday_closed(self):
        assert is_market_open(datetime(2026, 3, 8)) is False

    def test_holiday_closed(self):
        # MLK Day 2026
        assert is_market_open(datetime(2026, 1, 19)) is False

    def test_christmas_closed(self):
        assert is_market_open(datetime(2026, 12, 25)) is False


# ── get_last_n_trading_days ─────────────────────────────────────────

class TestGetLastNTradingDays:
    def test_returns_n_days(self):
        # Wednesday at 17:00 (after close)
        dt = datetime(2026, 3, 4, 17, 0)
        days = get_last_n_trading_days(3, dt)
        assert len(days) == 3

    def test_skips_weekends(self):
        # Monday at 10am (before close) — should start from previous Friday
        dt = datetime(2026, 3, 2, 10, 0)
        days = get_last_n_trading_days(1, dt)
        assert days[0] == "2026-02-27"  # Friday

    def test_skips_holidays(self):
        # Day after MLK Day (Tuesday 2026-01-20 at 10am)
        dt = datetime(2026, 1, 20, 10, 0)
        days = get_last_n_trading_days(2, dt)
        # Should skip MLK Day (Jan 19) and weekend
        assert "2026-01-19" not in days
        assert len(days) == 2


# ── analyze_darkpool ────────────────────────────────────────────────

class TestAnalyzeDarkpool:
    def test_empty_trades_no_data(self):
        result = analyze_darkpool([])
        assert result["flow_direction"] == "NO_DATA"
        assert result["total_volume"] == 0
        assert result["dp_buy_ratio"] is None

    def test_canceled_trades_skipped(self):
        trades = [
            {"size": "1000", "price": "50", "premium": "50000",
             "nbbo_bid": "49", "nbbo_ask": "51", "canceled": True},
        ]
        result = analyze_darkpool(trades)
        assert result["num_prints"] == 0
        assert result["total_volume"] == 0

    def test_strong_buy_accumulation(self):
        # All trades above midpoint -> buy volume
        trades = [
            {"size": "1000", "price": "51", "premium": "51000",
             "nbbo_bid": "49", "nbbo_ask": "51"},
            {"size": "2000", "price": "50.5", "premium": "101000",
             "nbbo_bid": "49", "nbbo_ask": "51"},
        ]
        result = analyze_darkpool(trades)
        assert result["flow_direction"] == "ACCUMULATION"
        assert result["dp_buy_ratio"] == 1.0
        assert result["flow_strength"] == 100.0

    def test_strong_sell_distribution(self):
        # All trades below midpoint -> sell volume
        trades = [
            {"size": "1000", "price": "49", "premium": "49000",
             "nbbo_bid": "49", "nbbo_ask": "51"},
            {"size": "2000", "price": "49.5", "premium": "99000",
             "nbbo_bid": "49", "nbbo_ask": "51"},
        ]
        result = analyze_darkpool(trades)
        assert result["flow_direction"] == "DISTRIBUTION"
        assert result["dp_buy_ratio"] == 0.0
        assert result["flow_strength"] == 100.0

    def test_balanced_neutral(self):
        # Equal buy/sell -> NEUTRAL
        trades = [
            {"size": "1000", "price": "51", "premium": "51000",
             "nbbo_bid": "49", "nbbo_ask": "51"},
            {"size": "1000", "price": "49", "premium": "49000",
             "nbbo_bid": "49", "nbbo_ask": "51"},
        ]
        result = analyze_darkpool(trades)
        assert result["flow_direction"] == "NEUTRAL"
        assert result["dp_buy_ratio"] == 0.5

    def test_no_nbbo_unknown(self):
        trades = [
            {"size": "1000", "price": "50", "premium": "50000",
             "nbbo_bid": "0", "nbbo_ask": "0"},
        ]
        result = analyze_darkpool(trades)
        assert result["flow_direction"] == "UNKNOWN"
        assert result["dp_buy_ratio"] is None

    def test_num_prints_excludes_canceled(self):
        trades = [
            {"size": "1000", "price": "51", "premium": "51000",
             "nbbo_bid": "49", "nbbo_ask": "51"},
            {"size": "500", "price": "49", "premium": "24500",
             "nbbo_bid": "49", "nbbo_ask": "51", "canceled": True},
        ]
        result = analyze_darkpool(trades)
        assert result["num_prints"] == 1


# ── analyze_options_flow ────────────────────────────────────────────

class TestAnalyzeOptionsFlow:
    def test_empty_alerts_no_data(self):
        result = analyze_options_flow([])
        assert result["bias"] == "NO_DATA"
        assert result["total_alerts"] == 0

    def test_all_calls_bias(self):
        alerts = [
            {"premium": "100000", "is_call": True},
            {"premium": "200000", "is_call": True},
        ]
        result = analyze_options_flow(alerts)
        assert result["bias"] == "ALL_CALLS"
        assert result["call_put_ratio"] is None

    def test_strongly_bullish(self):
        # Call premium >> Put premium  (ratio >= 2.0)
        alerts = [
            {"premium": "300000", "is_call": True},
            {"premium": "100000", "is_call": False},
        ]
        result = analyze_options_flow(alerts)
        assert result["bias"] == "STRONGLY_BULLISH"
        assert result["call_put_ratio"] == 3.0

    def test_strongly_bearish(self):
        # Put premium >> Call premium  (ratio <= 0.5)
        alerts = [
            {"premium": "100000", "is_call": True},
            {"premium": "400000", "is_call": False},
        ]
        result = analyze_options_flow(alerts)
        assert result["bias"] == "STRONGLY_BEARISH"
        assert result["call_put_ratio"] == 0.25

    def test_bullish(self):
        # Call/put ratio between 1.2 and 2.0
        alerts = [
            {"premium": "150000", "is_call": True},
            {"premium": "100000", "is_call": False},
        ]
        result = analyze_options_flow(alerts)
        assert result["bias"] == "BULLISH"
        assert result["call_put_ratio"] == 1.5

    def test_bearish(self):
        # Call/put ratio between 0.5 and 0.8
        alerts = [
            {"premium": "70000", "is_call": True},
            {"premium": "100000", "is_call": False},
        ]
        result = analyze_options_flow(alerts)
        assert result["bias"] == "BEARISH"
        assert result["call_put_ratio"] == 0.7

    def test_neutral(self):
        # Call/put ratio between 0.8 and 1.2
        alerts = [
            {"premium": "100000", "is_call": True},
            {"premium": "100000", "is_call": False},
        ]
        result = analyze_options_flow(alerts)
        assert result["bias"] == "NEUTRAL"
        assert result["call_put_ratio"] == 1.0


# ── fetch_flow combined signal ──────────────────────────────────────

class TestFetchFlowCombinedSignal:
    """Test the combined signal logic from fetch_flow() by mocking I/O."""

    @patch("fetch_flow.UWClient")
    @patch("fetch_flow.fetch_flow_alerts", return_value=[])
    @patch("fetch_flow.fetch_darkpool", return_value=[])
    @patch("fetch_flow.get_last_n_trading_days", return_value=["2026-03-02"])
    def test_no_data_no_signal(self, mock_days, mock_dp, mock_flow, MockUWClient):
        mock_client = MagicMock()
        MockUWClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockUWClient.return_value.__exit__ = MagicMock(return_value=False)
        from fetch_flow import fetch_flow
        result = fetch_flow("AAPL", lookback_days=1)
        assert result["combined_signal"] == "NO_SIGNAL"

    @patch("fetch_flow.UWClient")
    @patch("fetch_flow.fetch_flow_alerts")
    @patch("fetch_flow.fetch_darkpool")
    @patch("fetch_flow.get_last_n_trading_days", return_value=["2026-03-02"])
    def test_bullish_confluence(self, mock_days, mock_dp, mock_flow, MockUWClient):
        mock_client = MagicMock()
        MockUWClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockUWClient.return_value.__exit__ = MagicMock(return_value=False)
        from fetch_flow import fetch_flow
        # DP: strong buy -> ACCUMULATION
        mock_dp.return_value = [
            {"size": "5000", "price": "51", "premium": "255000",
             "nbbo_bid": "49", "nbbo_ask": "51"},
        ]
        # Options: strongly bullish
        mock_flow.return_value = [
            {"premium": "300000", "is_call": True},
            {"premium": "100000", "is_call": False},
        ]
        result = fetch_flow("AAPL", lookback_days=1)
        assert result["combined_signal"] == "STRONG_BULLISH_CONFLUENCE"

    @patch("fetch_flow.UWClient")
    @patch("fetch_flow.fetch_flow_alerts")
    @patch("fetch_flow.fetch_darkpool")
    @patch("fetch_flow.get_last_n_trading_days", return_value=["2026-03-02"])
    def test_dp_only_signal(self, mock_days, mock_dp, mock_flow, MockUWClient):
        mock_client = MagicMock()
        MockUWClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockUWClient.return_value.__exit__ = MagicMock(return_value=False)
        from fetch_flow import fetch_flow
        mock_dp.return_value = [
            {"size": "5000", "price": "51", "premium": "255000",
             "nbbo_bid": "49", "nbbo_ask": "51"},
        ]
        mock_flow.return_value = []
        result = fetch_flow("AAPL", lookback_days=1)
        assert result["combined_signal"] == "DP_ACCUMULATION_ONLY"
