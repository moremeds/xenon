"""Tests for the parquet-mirror-backed TAService."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest


def _write_minimal_parquet(mirror: Path, ticker: str = "AAPL", timeframe: str = "1d", n: int = 260):
    from scripts.apex_refresh import _compute_indicators_adapter
    from scripts.ta_lib.parquet_store import write_indicators, write_ohlcv

    ts = pd.date_range("2024-01-01", periods=n, freq="B", tz="UTC")
    close = [100.0 + i * 0.1 for i in range(n)]
    ohlcv = pd.DataFrame(
        {
            "timestamp": ts,
            "open": close,
            "high": [c + 0.5 for c in close],
            "low": [c - 0.5 for c in close],
            "close": close,
            "volume": [1_000_000] * n,
        }
    )
    ind = _compute_indicators_adapter(ohlcv)
    hist = mirror / "parquet" / "historical" / timeframe / f"{ticker}.parquet"
    ind_p = mirror / "parquet" / "indicators" / timeframe / f"{ticker}.parquet"
    hist.parent.mkdir(parents=True, exist_ok=True)
    ind_p.parent.mkdir(parents=True, exist_ok=True)
    write_ohlcv(str(hist), ohlcv, timeframe=timeframe)
    write_indicators(str(ind_p), ind, timeframe=timeframe)


def test_get_ohlcv_returns_none_when_missing(tmp_path):
    from scripts.ta_lib.service import TAService

    svc = TAService(mirror_dir=tmp_path)
    assert svc.get_ohlcv("AAPL") is None


def test_get_indicators_returns_none_when_missing(tmp_path):
    from scripts.ta_lib.service import TAService

    svc = TAService(mirror_dir=tmp_path)
    assert svc.get_indicators("AAPL") is None


def test_get_snapshot_returns_none_when_only_ohlcv_present(tmp_path):
    """If indicators parquet is absent, get_snapshot returns None (not a partial dict)."""
    from scripts.ta_lib.parquet_store import write_ohlcv
    from scripts.ta_lib.service import TAService

    ts = pd.date_range("2024-01-01", periods=30, freq="B", tz="UTC")
    ohlcv = pd.DataFrame(
        {
            "timestamp": ts,
            "open": [100.0] * 30,
            "high": [101.0] * 30,
            "low": [99.0] * 30,
            "close": [100.5] * 30,
            "volume": [1_000_000] * 30,
        }
    )
    hist = tmp_path / "parquet" / "historical" / "1d" / "AAPL.parquet"
    hist.parent.mkdir(parents=True, exist_ok=True)
    write_ohlcv(str(hist), ohlcv, timeframe="1d")

    svc = TAService(mirror_dir=tmp_path)
    assert svc.get_snapshot("AAPL") is None


def test_get_ohlcv_returns_dataframe_when_present(tmp_path):
    from scripts.ta_lib.service import TAService

    _write_minimal_parquet(tmp_path)
    svc = TAService(mirror_dir=tmp_path)
    df = svc.get_ohlcv("AAPL")
    assert df is not None
    assert "close" in df.columns
    assert len(df) == 260


def test_rename_map_applied_in_snapshot(tmp_path):
    """Scanner expects 'rsi' not 'rsi_14', 'ma_20' not 'sma_20', 'bbw' not 'bb_width'."""
    from scripts.ta_lib.service import TAService

    _write_minimal_parquet(tmp_path)
    svc = TAService(mirror_dir=tmp_path)
    snap = svc.get_snapshot("AAPL")
    assert "rsi" in snap and "rsi_14" not in snap
    assert "ma_20" in snap and "sma_20" not in snap
    assert "bbw" in snap and "bb_width" not in snap
