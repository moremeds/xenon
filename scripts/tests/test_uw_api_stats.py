"""Tests for UW API usage stats collector."""

import threading

import pytest

from utils.uw_api_stats import UWApiStats, _extract_ticker, _normalize_endpoint

# ── _extract_ticker ──────────────────────────────────────────────────


class TestExtractTicker:
    def test_stock_path(self):
        assert _extract_ticker("stock/AAPL/volatility") == "AAPL"

    def test_darkpool_path(self):
        assert _extract_ticker("darkpool/MSFT") == "MSFT"

    def test_earnings_path(self):
        assert _extract_ticker("earnings/TSLA") == "TSLA"

    def test_short_path(self):
        assert _extract_ticker("short/GME") == "GME"

    def test_etf_path(self):
        assert _extract_ticker("etf/SPY/holdings") == "SPY"

    def test_lit_flow_path(self):
        assert _extract_ticker("lit-flow/NVDA") == "NVDA"

    def test_dotted_ticker(self):
        assert _extract_ticker("stock/BRK.B/volatility") == "BRK.B"

    def test_flow_alerts_query_param(self):
        assert _extract_ticker("option-trades/flow-alerts", {"ticker_symbol": "AAPL"}) == "AAPL"

    def test_flow_alerts_comma_separated(self):
        assert _extract_ticker("option-trades/flow-alerts", {"ticker_symbol": "AAPL,MSFT,NVDA"}) == "AAPL"

    def test_short_screener_tickers_param(self):
        assert _extract_ticker("short_screener", {"tickers": "AAPL"}) == "AAPL"

    def test_market_endpoint_no_ticker(self):
        assert _extract_ticker("market/correlations") == "_market"

    def test_screener_no_params(self):
        assert _extract_ticker("screener/stocks") == "_market"

    def test_no_params_none(self):
        assert _extract_ticker("stock-directory/ticker-exchanges", None) == "_market"


# ── _normalize_endpoint ──────────────────────────────────────────────


class TestNormalizeEndpoint:
    def test_stock_endpoint(self):
        assert _normalize_endpoint("stock/AAPL/volatility") == "stock/*/volatility"

    def test_darkpool_endpoint(self):
        assert _normalize_endpoint("darkpool/MSFT") == "darkpool/*"

    def test_no_ticker_passthrough(self):
        assert _normalize_endpoint("option-trades/flow-alerts") == "option-trades/flow-alerts"

    def test_earnings(self):
        assert _normalize_endpoint("earnings/TSLA") == "earnings/*"


# ── UWApiStats ───────────────────────────────────────────────────────


class TestUWApiStats:
    @pytest.fixture
    def s(self, tmp_path):
        # Isolate from the real data/uw_api_stats_history.json — otherwise
        # _load_history() rehydrates session counters from whatever the
        # live FastAPI process wrote last, and "initial state" is no
        # longer initial.
        return UWApiStats(history_path=tmp_path / "uw_api_stats_history.json")

    def test_initial_state(self, s):
        stats = s.get_stats()
        assert stats["totals"]["requests"] == 0
        assert stats["totals"]["success"] == 0
        assert stats["latency_ms"]["samples"] == 0
        assert "session_started_at" in stats

    def test_record_success(self, s):
        s.record("stock/AAPL/volatility", status=200, latency_ms=150.0)
        stats = s.get_stats()
        assert stats["totals"]["requests"] == 1
        assert stats["totals"]["success"] == 1
        assert stats["latency_ms"]["samples"] == 1
        assert stats["latency_ms"]["avg"] == 150.0
        assert stats["by_status"][200] == 1
        assert stats["by_ticker"]["AAPL"]["requests"] == 1
        assert stats["by_ticker"]["AAPL"]["success"] == 1
        assert stats["by_endpoint_type"]["stock/*/volatility"]["requests"] == 1

    def test_record_cached(self, s):
        s.record("stock/AAPL/volatility", status=200, cached=True)
        stats = s.get_stats()
        assert stats["totals"]["requests"] == 1
        assert stats["totals"]["cached"] == 1
        assert stats["totals"]["success"] == 0  # cached != success counter
        assert stats["latency_ms"]["samples"] == 0  # no latency for cache hit
        assert stats["by_ticker"]["AAPL"]["cached"] == 1

    def test_record_rate_limit(self, s):
        s.record("stock/AAPL/volatility", status=429, latency_ms=50.0, retried=True)
        stats = s.get_stats()
        assert stats["totals"]["rate_limits"] == 1
        assert stats["totals"]["failures"] == 1
        assert stats["totals"]["retries"] == 1
        assert stats["by_status"][429] == 1

    def test_record_connection_error(self, s):
        s.record("stock/AAPL/volatility", connection_error=True, latency_ms=30000.0)
        stats = s.get_stats()
        assert stats["totals"]["connection_errors"] == 1
        assert stats["totals"]["failures"] == 1

    def test_record_404(self, s):
        s.record("stock/ZZZZZ/volatility", status=404, latency_ms=80.0)
        stats = s.get_stats()
        assert stats["totals"]["failures"] == 1
        assert stats["by_status"][404] == 1

    def test_multiple_tickers(self, s):
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        s.record("stock/MSFT/volatility", status=200, latency_ms=200.0)
        s.record("stock/AAPL/greek-exposure", status=200, latency_ms=150.0)
        stats = s.get_stats()
        assert stats["totals"]["requests"] == 3
        assert stats["by_ticker"]["AAPL"]["requests"] == 2
        assert stats["by_ticker"]["MSFT"]["requests"] == 1
        assert stats["by_endpoint_type"]["stock/*/volatility"]["requests"] == 2
        assert stats["by_endpoint_type"]["stock/*/greek-exposure"]["requests"] == 1

    def test_latency_p95(self, s):
        for i in range(100):
            s.record("stock/AAPL/volatility", status=200, latency_ms=float(i + 1))
        stats = s.get_stats()
        assert stats["latency_ms"]["samples"] == 100
        assert stats["latency_ms"]["min"] == 1.0
        assert stats["latency_ms"]["max"] == 100.0
        assert stats["latency_ms"]["p95"] == 96.0  # index int(100*0.95)=95 → value 96

    def test_latency_window_bounded(self, s):
        # Record more than _LATENCY_WINDOW samples
        for i in range(1200):
            s.record("stock/AAPL/volatility", status=200, latency_ms=float(i))
        stats = s.get_stats()
        # Window is 1000, so only last 1000 samples retained
        assert stats["latency_ms"]["samples"] == 1000

    def test_reset(self, s):
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        s.record("stock/MSFT/volatility", status=429, latency_ms=50.0)
        s.reset()
        stats = s.get_stats()
        assert stats["totals"]["requests"] == 0
        assert stats["by_ticker"] == {}
        assert stats["by_endpoint_type"] == {}
        assert stats["latency_ms"]["samples"] == 0

    def test_flow_alerts_with_params(self, s):
        s.record(
            "option-trades/flow-alerts",
            params={"ticker_symbol": "AAPL"},
            status=200,
            latency_ms=200.0,
        )
        stats = s.get_stats()
        assert stats["by_ticker"]["AAPL"]["requests"] == 1

    def test_market_endpoint(self, s):
        s.record("market/correlations", status=200, latency_ms=300.0)
        stats = s.get_stats()
        assert stats["by_ticker"]["_market"]["requests"] == 1

    def test_uptime_present(self, s):
        stats = s.get_stats()
        assert "uptime_seconds" in stats
        assert stats["uptime_seconds"] >= 0

    def test_thread_safety(self, s):
        """Verify no crashes under concurrent access."""
        errors = []

        def worker(tid):
            try:
                for i in range(200):
                    s.record(f"stock/T{tid}/volatility", status=200, latency_ms=float(i))
                    if i % 50 == 0:
                        s.get_stats()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        stats = s.get_stats()
        assert stats["totals"]["requests"] == 8 * 200
