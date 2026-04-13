"""Contract test: TAService.get_snapshot() must return all keys that trend_scan.py expects."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

# These are the exact keys that trend_scan.py's _stage_a, _trend_summary,
# and TrendCandidate construction read from fetch_ohlcv().
# Extracted from scripts/trend_scan.py lines 369-392 and 495-512.
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
    # NOTE: market_cap is NOT here — it comes from stock_info, not TAService
    # NOTE: rs_vs_spy is NOT here — it's computed cross-ticker in trend_scan.py
}


def _make_bar_data(n: int = 260) -> list:
    np.random.seed(42)
    bars = []
    from datetime import datetime

    for i in range(n):
        current = datetime(2025, 5, 1) + pd.tseries.offsets.BDay(i)
        close = 100.0 + i * 0.1 + np.random.randn() * 0.3
        bars.append(
            SimpleNamespace(
                date=current.strftime("%Y%m%d"),
                open=close - 0.1,
                high=close + 0.5,
                low=close - 0.5,
                close=close,
                volume=1_000_000,
            )
        )
    return bars


@pytest.fixture
def ta_service():
    from scripts.ta_lib.service import TAService

    mock_ib = MagicMock()
    mock_ib.get_historical_data.return_value = _make_bar_data(n=260)
    mock_ib._ib = MagicMock()
    mock_ib._ib.qualifyContracts.return_value = [MagicMock()]
    return TAService(db_path=":memory:", ib_client=mock_ib)


class TestSnapshotContract:
    def test_all_required_keys_present(self, ta_service):
        snapshot = ta_service.get_snapshot("AAPL")
        missing = REQUIRED_KEYS - set(snapshot.keys())
        assert not missing, f"Missing keys in get_snapshot(): {missing}"

    def test_no_nan_in_scalar_fields(self, ta_service):
        snapshot = ta_service.get_snapshot("AAPL")
        scalar_keys = REQUIRED_KEYS - {"ma_20_series", "ticker"}
        for key in scalar_keys:
            val = snapshot[key]
            assert not (isinstance(val, float) and np.isnan(val)), (
                f"snapshot['{key}'] is NaN — trend_scan scoring will break"
            )

    def test_ma_20_series_is_list_of_floats(self, ta_service):
        snapshot = ta_service.get_snapshot("AAPL")
        series = snapshot["ma_20_series"]
        assert isinstance(series, list)
        assert all(isinstance(v, float) for v in series)
        assert 1 <= len(series) <= 5

    def test_values_are_reasonable(self, ta_service):
        snapshot = ta_service.get_snapshot("AAPL")
        assert snapshot["close"] > 0
        assert snapshot["price"] == snapshot["close"]
        assert 0 <= snapshot["rsi"] <= 100
        assert snapshot["adx"] >= 0
        assert snapshot["avg_20d_volume"] > 0
        assert snapshot["dollar_volume"] > 0
        assert snapshot["high_52w"] >= snapshot["close"]

    def test_ticker_is_uppercase(self, ta_service):
        snapshot = ta_service.get_snapshot("aapl")
        assert snapshot["ticker"] == "AAPL"
