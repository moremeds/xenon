"""Unit tests for ta_lib.indicators."""

import numpy as np
import pandas as pd
import pytest


def _make_ohlcv_df(n: int = 260, base_close: float = 100.0) -> pd.DataFrame:
    """Generate a synthetic OHLCV DataFrame with a gentle uptrend."""
    np.random.seed(42)
    closes = base_close + np.cumsum(np.random.randn(n) * 0.5)
    highs = closes + np.abs(np.random.randn(n) * 0.3)
    lows = closes - np.abs(np.random.randn(n) * 0.3)
    opens = closes + np.random.randn(n) * 0.1
    volumes = np.random.randint(500_000, 2_000_000, size=n)
    dates = pd.bdate_range(end="2026-04-10", periods=n)
    return pd.DataFrame(
        {
            "date": dates,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


class TestComputeAll:
    def test_sma_columns_present(self):
        from scripts.ta_lib.indicators import compute_all

        df = _make_ohlcv_df()
        result = compute_all(df)
        assert "sma_20" in result.columns
        assert "sma_50" in result.columns
        assert "sma_200" in result.columns

    def test_sma_20_value(self):
        from scripts.ta_lib.indicators import compute_all

        df = _make_ohlcv_df()
        result = compute_all(df)
        # SMA(20) at last row should equal mean of last 20 closes
        expected = df["close"].iloc[-20:].mean()
        assert result["sma_20"].iloc[-1] == pytest.approx(expected, rel=1e-6)

    def test_sma_warmup_is_nan(self):
        from scripts.ta_lib.indicators import compute_all

        df = _make_ohlcv_df()
        result = compute_all(df)
        # First 199 rows of sma_200 should be NaN (0-indexed)
        assert np.isnan(result["sma_200"].iloc[0])
        assert not np.isnan(result["sma_200"].iloc[-1])


class TestRsiEdgeCases:
    def test_flat_series_rsi_is_50(self):
        from scripts.ta_lib.indicators import compute_all

        df = _make_ohlcv_df()
        # Make all closes identical
        df["close"] = 100.0
        df["high"] = 100.5
        df["low"] = 99.5
        df["open"] = 100.0
        result = compute_all(df)
        # After warmup, RSI on flat series should be 50.0
        last_rsi = result["rsi_14"].iloc[-1]
        assert last_rsi == pytest.approx(50.0, abs=0.1)

    def test_all_up_rsi_near_100(self):
        from scripts.ta_lib.indicators import compute_all

        df = _make_ohlcv_df()
        # Monotonically increasing closes
        df["close"] = np.linspace(50, 200, len(df))
        df["high"] = df["close"] + 1
        df["low"] = df["close"] - 1
        df["open"] = df["close"] - 0.5
        result = compute_all(df)
        last_rsi = result["rsi_14"].iloc[-1]
        assert last_rsi > 95.0

    def test_all_down_rsi_near_0(self):
        from scripts.ta_lib.indicators import compute_all

        df = _make_ohlcv_df()
        # Monotonically decreasing closes
        df["close"] = np.linspace(200, 50, len(df))
        df["high"] = df["close"] + 1
        df["low"] = df["close"] - 1
        df["open"] = df["close"] + 0.5
        result = compute_all(df)
        last_rsi = result["rsi_14"].iloc[-1]
        assert last_rsi < 5.0


class TestDerivedColumns:
    def test_bb_width_computed(self):
        from scripts.ta_lib.indicators import compute_all

        df = _make_ohlcv_df()
        result = compute_all(df)
        assert "bb_width" in result.columns
        last = result["bb_width"].iloc[-1]
        assert last > 0  # uptrend with variance → positive bb_width

    def test_bb_width_matches_manual(self):
        from scripts.ta_lib.indicators import compute_all

        df = _make_ohlcv_df()
        result = compute_all(df)
        row = result.iloc[-1]
        expected = (row["bb_upper"] - row["bb_lower"]) / row["bb_middle"]
        assert row["bb_width"] == pytest.approx(expected, rel=1e-6)

    def test_all_indicator_columns_present(self):
        from scripts.ta_lib.indicators import compute_all

        df = _make_ohlcv_df()
        result = compute_all(df)
        expected_cols = {
            "sma_20",
            "sma_50",
            "sma_200",
            "rsi_14",
            "macd",
            "macd_signal",
            "macd_histogram",
            "adx_14",
            "bb_upper",
            "bb_middle",
            "bb_lower",
            "bb_width",
            "atr_14",
        }
        assert expected_cols.issubset(set(result.columns))

    def test_short_series_all_nan(self):
        from scripts.ta_lib.indicators import compute_all

        df = _make_ohlcv_df(n=10)
        result = compute_all(df)
        # 10 bars — SMA(200) should be all NaN
        assert result["sma_200"].isna().all()
        # SMA(20) should also be all NaN (need 20 bars)
        assert result["sma_20"].isna().all()
