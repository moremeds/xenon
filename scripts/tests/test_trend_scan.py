"""Tests for Stage A universe-join filtering in scripts.trend_scan."""

from __future__ import annotations

from pathlib import Path


def test_stage_a_data_rejects_below_market_cap():
    from scripts.trend_scan import _stage_a_data
    from scripts.trend_scan_lib.config import TrendScanConfig

    cfg = TrendScanConfig(min_market_cap=1_000_000_000, min_dollar_volume=10_000_000, min_price=5.0)
    universe_row = {"symbol": "AAPL", "marketCap": 5e8, "dollar_volume": 2e10, "tier": "mid_cap"}
    ohlcv = {"close": 100.0, "dollar_volume": 2e10, "price": 100.0}

    class _FakeFetcher:
        def fetch_ohlcv(self, ticker: str) -> dict:
            return ohlcv

    assert _stage_a_data("AAPL", universe_row, _FakeFetcher(), cfg) is None


def test_stage_a_data_rejects_excluded_tier():
    from scripts.trend_scan import _stage_a_data
    from scripts.trend_scan_lib.config import TrendScanConfig

    cfg = TrendScanConfig(exclude_tiers={"leveraged_etf"})
    universe_row = {"symbol": "TQQQ", "marketCap": 1e10, "dollar_volume": 1e10, "tier": "leveraged_etf"}
    ohlcv = {"close": 50.0, "dollar_volume": 1e10, "price": 50.0}

    class _FakeFetcher:
        def fetch_ohlcv(self, ticker: str) -> dict:
            return ohlcv

    assert _stage_a_data("TQQQ", universe_row, _FakeFetcher(), cfg) is None


def test_stage_a_data_rejects_below_turnover_rate():
    from scripts.trend_scan import _stage_a_data
    from scripts.trend_scan_lib.config import TrendScanConfig

    cfg = TrendScanConfig(min_turnover_rate=0.05)  # 5% turnover floor
    universe_row = {
        "symbol": "SLOW",
        "marketCap": 5e9,
        "dollar_volume": 5e8,
        "tier": "mid_cap",
        "turnover_rate": 0.01,
    }
    ohlcv = {"close": 50.0, "dollar_volume": 5e8, "price": 50.0}

    class _FakeFetcher:
        def fetch_ohlcv(self, ticker: str) -> dict:
            return ohlcv

    assert _stage_a_data("SLOW", universe_row, _FakeFetcher(), cfg) is None


def test_stage_a_data_passes_when_all_floors_met():
    from scripts.trend_scan import _stage_a_data
    from scripts.trend_scan_lib.config import TrendScanConfig

    cfg = TrendScanConfig()
    universe_row = {
        "symbol": "AAPL",
        "marketCap": 3e12,
        "dollar_volume": 2e10,
        "tier": "mega_cap",
        "turnover_rate": 0.02,
    }
    ohlcv = {"close": 200.0, "dollar_volume": 2e10, "price": 200.0}

    class _FakeFetcher:
        def fetch_ohlcv(self, ticker: str) -> dict:
            return ohlcv

    result = _stage_a_data("AAPL", universe_row, _FakeFetcher(), cfg)
    assert result is not None
    assert result["close"] == 200.0


def test_stage_a_data_rejects_below_min_price():
    """min_price is checked against OHLCV close (not universe row)."""
    from scripts.trend_scan import _stage_a_data
    from scripts.trend_scan_lib.config import TrendScanConfig

    cfg = TrendScanConfig(min_price=5.0)
    universe_row = {"symbol": "LOW", "marketCap": 5e9, "dollar_volume": 5e8, "tier": "mid_cap"}
    ohlcv = {"close": 2.50, "dollar_volume": 5e8, "price": 2.50}

    class _FakeFetcher:
        def fetch_ohlcv(self, ticker: str) -> dict:
            return ohlcv

    assert _stage_a_data("LOW", universe_row, _FakeFetcher(), cfg) is None


def test_filter_universe_to_covered_a19(tmp_path: Path):
    """A19: tickers missing a parquet file are filtered out and reported as missing."""
    from scripts.trend_scan import _filter_universe_to_covered

    (tmp_path / "parquet" / "historical" / "1d").mkdir(parents=True)
    (tmp_path / "parquet" / "historical" / "1d" / "AAPL.parquet").write_bytes(b"fake")

    universe = [
        {"symbol": "AAPL", "marketCap": 3e12},
        {"symbol": "NEWCO", "marketCap": 1e9},
        {"symbol": "OTHER", "marketCap": 2e9},
    ]
    covered, missing = _filter_universe_to_covered(tmp_path, universe, timeframes=("1d",))
    assert [r["symbol"] for r in covered] == ["AAPL"]
    assert set(missing) == {"NEWCO", "OTHER"}
