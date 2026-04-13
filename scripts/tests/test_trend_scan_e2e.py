"""End-to-end tests for trend_scan.py pipeline."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


def _mock_ohlcv_data(ticker: str, bullish: bool = True) -> dict:
    if bullish:
        return {
            "ticker": ticker,
            "close": 150,
            "ma_20": 145,
            "ma_50": 140,
            "ma_200": 130,
            "rsi": 62,
            "adx": 32,
            "macd": 1.5,
            "macd_signal": 1.0,
            "macd_histogram": 0.5,
            "rs_vs_spy": 1.15,
            "ma_20_series": [140, 141, 142, 143, 145],
            "recent_avg_volume": 1_500_000,
            "avg_20d_volume": 1_000_000,
            "recent_up_ratio": 0.7,
            "bbw": 0.05,
            "high_52w": 152,
            "range_20d_pct": 0.04,
            "atr_pct": 0.015,
            "dollar_volume": 20_000_000,
            "market_cap": 2_000_000_000,
            "price": 150,
        }
    return {
        "ticker": ticker,
        "close": 120,
        "ma_20": 130,
        "ma_50": 140,
        "ma_200": 150,
        "rsi": 35,
        "adx": 12,
        "macd": -1.0,
        "macd_signal": 0.5,
        "macd_histogram": -1.5,
        "rs_vs_spy": 0.85,
        "ma_20_series": [145, 144, 143, 142, 140],
        "recent_avg_volume": 800_000,
        "avg_20d_volume": 1_000_000,
        "recent_up_ratio": 0.3,
        "bbw": 0.18,
        "high_52w": 170,
        "range_20d_pct": 0.12,
        "atr_pct": 0.02,
        "dollar_volume": 20_000_000,
        "market_cap": 2_000_000_000,
        "price": 120,
    }


def _mock_structure_data() -> dict:
    return {
        "spot": 150,
        "gamma_flip": 145,
        "call_wall": 165,
        "put_wall": 146,
        "max_pain": 148,
        "net_gex": 200_000,
        "net_call_oi_change": 3000,
        "net_put_oi_change": -1000,
        "gex_at_spot": 50_000,
    }


def _mock_vol_data() -> dict:
    return {"iv_rank": 22, "term_structure": "normal", "iv_rv_ratio": 0.94}


def _mock_flow_data() -> dict:
    return {
        "ask_dominance": 0.85,
        "flow_count": 5,
        "expiry_cluster_ratio": 0.8,
        "avg_strike_pct_otm": 0.04,
        "net_delta": 40_000,
        "net_vega": 20_000,
        "dp_direction": "bullish",
    }


def test_scan_pipeline_produces_output(tmp_path):
    from scripts.trend_scan import run_scan_pipeline
    from scripts.trend_scan_lib.config import TrendScanConfig

    sp = tmp_path / "sp500.json"
    nq = tmp_path / "nasdaq100.json"
    sp.write_text(json.dumps(["AAPL", "NVDA"]))
    nq.write_text(json.dumps([]))
    cfg = TrendScanConfig(top_n=5, sp500_path=str(sp), nasdaq100_path=str(nq))
    mock_data_fetcher = MagicMock()
    mock_data_fetcher.fetch_ohlcv.side_effect = lambda t: _mock_ohlcv_data(t, bullish=True)
    mock_data_fetcher.fetch_structure.side_effect = lambda t: _mock_structure_data()
    mock_data_fetcher.fetch_volatility.side_effect = lambda t: _mock_vol_data()
    mock_data_fetcher.fetch_flow.side_effect = lambda t: _mock_flow_data()
    mock_data_fetcher.fetch_market_context.return_value = {"spy_close": 520.0, "vix_close": 17.0, "regime": "bullish"}
    result = run_scan_pipeline(cfg, data_fetcher=mock_data_fetcher, uw_client=None, ib_client=None, db_path=":memory:")
    assert "scan_id" in result
    assert "scan_timestamp" in result
    assert "candidates" in result
    assert len(result["candidates"]) <= 5
    assert result["universe_size"] == 2


def test_scan_pipeline_filters_weak_tickers(tmp_path):
    from scripts.trend_scan import run_scan_pipeline
    from scripts.trend_scan_lib.config import TrendScanConfig

    sp = tmp_path / "sp500.json"
    nq = tmp_path / "nasdaq100.json"
    sp.write_text(json.dumps(["GOOD", "BAD"]))
    nq.write_text(json.dumps([]))
    cfg = TrendScanConfig(top_n=5, sp500_path=str(sp), nasdaq100_path=str(nq))
    mock_data_fetcher = MagicMock()
    mock_data_fetcher.fetch_ohlcv.side_effect = lambda t: _mock_ohlcv_data(t, bullish=(t == "GOOD"))
    mock_data_fetcher.fetch_structure.side_effect = lambda t: _mock_structure_data()
    mock_data_fetcher.fetch_volatility.side_effect = lambda t: _mock_vol_data()
    mock_data_fetcher.fetch_flow.side_effect = lambda t: _mock_flow_data()
    mock_data_fetcher.fetch_market_context.return_value = {"spy_close": 520.0, "vix_close": 17.0, "regime": "bullish"}
    result = run_scan_pipeline(cfg, data_fetcher=mock_data_fetcher, uw_client=None, ib_client=None, db_path=":memory:")
    tickers = [c["ticker"] for c in result["candidates"]]
    assert "BAD" not in tickers


def test_scan_output_has_required_fields(tmp_path):
    from scripts.trend_scan import run_scan_pipeline
    from scripts.trend_scan_lib.config import TrendScanConfig

    sp = tmp_path / "sp500.json"
    nq = tmp_path / "nasdaq100.json"
    sp.write_text(json.dumps(["NVDA"]))
    nq.write_text(json.dumps([]))
    cfg = TrendScanConfig(top_n=5, sp500_path=str(sp), nasdaq100_path=str(nq))
    mock_data_fetcher = MagicMock()
    mock_data_fetcher.fetch_ohlcv.side_effect = lambda t: _mock_ohlcv_data(t, bullish=True)
    mock_data_fetcher.fetch_structure.side_effect = lambda t: _mock_structure_data()
    mock_data_fetcher.fetch_volatility.side_effect = lambda t: _mock_vol_data()
    mock_data_fetcher.fetch_flow.side_effect = lambda t: _mock_flow_data()
    mock_data_fetcher.fetch_market_context.return_value = {"spy_close": 520.0, "vix_close": 17.0, "regime": "bullish"}
    result = run_scan_pipeline(cfg, data_fetcher=mock_data_fetcher, uw_client=None, ib_client=None, db_path=":memory:")
    for key in [
        "scan_id",
        "scan_timestamp",
        "market_context",
        "universe_size",
        "stage_a_survivors",
        "stage_b_survivors",
        "candidates",
    ]:
        assert key in result, f"Missing top-level key: {key}"
    if result["candidates"]:
        c = result["candidates"][0]
        for key in [
            "ticker",
            "spot_price",
            "direction",
            "final_score",
            "scores",
            "indicators",
            "summaries",
            "suggested_trade",
            "invalidation",
            "flags",
            "holding_window",
            "snapshot_timestamp",
        ]:
            assert key in c, f"Missing candidate key: {key}"


def test_scan_writes_to_duckdb(tmp_path):
    import duckdb

    from scripts.trend_scan import run_scan_pipeline
    from scripts.trend_scan_lib.config import TrendScanConfig

    sp = tmp_path / "sp500.json"
    nq = tmp_path / "nasdaq100.json"
    sp.write_text(json.dumps(["NVDA"]))
    nq.write_text(json.dumps([]))
    db_path = str(tmp_path / "test.duckdb")
    cfg = TrendScanConfig(top_n=5, sp500_path=str(sp), nasdaq100_path=str(nq))
    mock_data_fetcher = MagicMock()
    mock_data_fetcher.fetch_ohlcv.side_effect = lambda t: _mock_ohlcv_data(t, bullish=True)
    mock_data_fetcher.fetch_structure.side_effect = lambda t: _mock_structure_data()
    mock_data_fetcher.fetch_volatility.side_effect = lambda t: _mock_vol_data()
    mock_data_fetcher.fetch_flow.side_effect = lambda t: _mock_flow_data()
    mock_data_fetcher.fetch_market_context.return_value = {"spy_close": 520.0, "vix_close": 17.0, "regime": "bullish"}
    run_scan_pipeline(cfg, data_fetcher=mock_data_fetcher, uw_client=None, ib_client=None, db_path=db_path)
    conn = duckdb.connect(db_path)
    runs = conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
    candidates = conn.execute("SELECT COUNT(*) FROM scan_candidates").fetchone()[0]
    conn.close()
    assert runs == 1
    assert candidates >= 1


class TestTAServiceIntegration:
    """Verify trend_scan works when wired to a real (mocked-IB) TAService."""

    def test_fetch_ohlcv_delegates_to_ta_service(self):
        from unittest.mock import MagicMock

        import pandas as pd

        mock_ta = MagicMock()
        mock_ta.get_snapshot.return_value = _mock_ohlcv_data("AAPL", bullish=True)
        mock_ta.get_indicators.return_value = pd.DataFrame(
            {
                "date": pd.bdate_range("2026-01-01", periods=30),
                "close": [150.0] * 30,
            }
        )

        from scripts.trend_scan import LiveTrendDataFetcher

        uw = MagicMock()
        uw.get_stock_info.return_value = {"data": {"marketcap": 2_000_000_000}}
        fetcher = LiveTrendDataFetcher(uw_client=uw, ta_service=mock_ta)
        fetcher._spy_df = pd.DataFrame(
            {
                "date": pd.bdate_range("2026-01-01", periods=30),
                "close": [450.0] * 30,
            }
        )
        result = fetcher.fetch_ohlcv("AAPL")

        mock_ta.get_snapshot.assert_called_once_with("AAPL", allow_fetch=False)
        assert result["ticker"] == "AAPL"
        assert "rs_vs_spy" in result
        assert "market_cap" in result
