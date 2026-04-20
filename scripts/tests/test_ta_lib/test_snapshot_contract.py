"""Contract test: TAService.get_snapshot() must return all keys that trend_scan.py expects."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REQUIRED_KEYS = {
    "ticker",
    "close",
    "ma_20",
    "ma_50",
    "ma_200",
    "rsi",
    "adx",
    "macd",
    "macd_signal",
    "macd_histogram",
    "ma_20_series",
    "recent_avg_volume",
    "avg_20d_volume",
    "recent_up_ratio",
    "bbw",
    "high_52w",
    "range_20d_pct",
    "atr_pct",
    "dollar_volume",
    "price",
}


def _build_mirror(tmp_path: Path, ticker: str = "AAPL", n: int = 260) -> Path:
    """Populate a parquet mirror with one ticker's OHLCV + computed indicators."""
    from xenon.fetchers.fetch_apex_data import _compute_indicators_adapter
    from scripts.ta_lib.parquet_store import write_indicators, write_ohlcv

    np.random.seed(42)
    ts = pd.date_range("2024-01-01", periods=n, freq="B", tz="UTC")  # B = business days
    close = 100.0 + np.arange(n) * 0.1 + np.random.randn(n) * 0.3
    ohlcv = pd.DataFrame(
        {
            "timestamp": ts,
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.random.randint(500_000, 2_000_000, size=n).astype(int),
        }
    )
    indicators = _compute_indicators_adapter(ohlcv)

    mirror = tmp_path / "apex_mirror"
    hist = mirror / "parquet" / "historical" / "1d" / f"{ticker}.parquet"
    ind_p = mirror / "parquet" / "indicators" / "1d" / f"{ticker}.parquet"
    hist.parent.mkdir(parents=True, exist_ok=True)
    ind_p.parent.mkdir(parents=True, exist_ok=True)
    write_ohlcv(str(hist), ohlcv, timeframe="1d")
    write_indicators(str(ind_p), indicators, timeframe="1d")
    return mirror


def test_snapshot_includes_all_required_keys(tmp_path):
    from scripts.ta_lib.service import TAService

    mirror = _build_mirror(tmp_path)
    svc = TAService(mirror_dir=mirror)
    snap = svc.get_snapshot("AAPL")
    assert snap is not None
    missing = REQUIRED_KEYS - set(snap.keys())
    assert not missing, f"get_snapshot missing keys: {missing}"


def test_snapshot_scalar_invariants(tmp_path):
    from scripts.ta_lib.service import TAService

    mirror = _build_mirror(tmp_path)
    svc = TAService(mirror_dir=mirror)
    snap = svc.get_snapshot("AAPL")
    assert snap["close"] > 0
    assert snap["price"] == snap["close"]
    assert 0 <= snap["rsi"] <= 100
    assert snap["adx"] >= 0
    assert snap["avg_20d_volume"] > 0
    assert snap["dollar_volume"] > 0
    assert snap["high_52w"] >= snap["close"]
    assert snap["ticker"] == "AAPL"


def test_snapshot_ma_20_series_is_list_of_last_5(tmp_path):
    from scripts.ta_lib.service import TAService

    mirror = _build_mirror(tmp_path)
    svc = TAService(mirror_dir=mirror)
    snap = svc.get_snapshot("AAPL")
    series = snap["ma_20_series"]
    assert isinstance(series, list)
    assert len(series) == 5
    assert all(isinstance(v, float) for v in series)


def test_snapshot_high_low_and_up_ratio_present(tmp_path):
    from scripts.ta_lib.service import TAService

    mirror = _build_mirror(tmp_path)
    svc = TAService(mirror_dir=mirror)
    snap = svc.get_snapshot("AAPL")
    for field in ("high_20d", "low_20d", "low_52w", "up_day_volume_ratio"):
        assert field in snap, field
    assert snap["high_20d"] >= snap["low_20d"] > 0


def test_snapshot_returns_none_for_missing_ticker(tmp_path):
    from scripts.ta_lib.service import TAService

    mirror = _build_mirror(tmp_path)
    svc = TAService(mirror_dir=mirror)
    assert svc.get_snapshot("ZZZZ") is None


def test_get_indicators_returns_dataframe(tmp_path):
    from scripts.ta_lib.service import TAService

    mirror = _build_mirror(tmp_path)
    svc = TAService(mirror_dir=mirror)
    df = svc.get_indicators("AAPL", timeframe="1d")
    assert df is not None
    assert len(df) == 260
