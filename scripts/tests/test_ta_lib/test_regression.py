"""Regression tests: document expected differences between pandas TA and TA-Lib.

ADX and ATR will differ significantly (Wilder's smoothing vs rolling average).
SMA and MACD should match closely. RSI uses different EMA initialization.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

talib = pytest.importorskip("talib")


def _make_price_series(n: int = 260, seed: int = 42) -> pd.DataFrame:
    np.random.seed(seed)
    closes = 100.0 + np.cumsum(np.random.randn(n) * 0.5)
    highs = closes + np.abs(np.random.randn(n) * 0.3)
    lows = closes - np.abs(np.random.randn(n) * 0.3)
    return pd.DataFrame(
        {
            "close": closes,
            "high": highs,
            "low": lows,
        }
    )


class TestSMARegression:
    """SMA should match exactly — both use simple rolling mean."""

    def test_sma_20_matches_pandas(self):
        df = _make_price_series()
        pandas_sma = df["close"].rolling(20).mean().iloc[-1]
        talib_sma = talib.SMA(df["close"].to_numpy(), timeperiod=20)[-1]
        assert pandas_sma == pytest.approx(talib_sma, rel=1e-10)

    def test_sma_200_matches_pandas(self):
        df = _make_price_series()
        pandas_sma = df["close"].rolling(200).mean().iloc[-1]
        talib_sma = talib.SMA(df["close"].to_numpy(), timeperiod=200)[-1]
        assert pandas_sma == pytest.approx(talib_sma, rel=1e-10)


class TestMACDRegression:
    """MACD should be close — both use EMA, but initialization may differ slightly."""

    def test_macd_close_to_pandas(self):
        df = _make_price_series()
        closes = df["close"]

        # Pandas MACD
        ema12 = closes.ewm(span=12, adjust=False).mean()
        ema26 = closes.ewm(span=26, adjust=False).mean()
        pandas_macd = (ema12 - ema26).iloc[-1]

        # TA-Lib MACD
        macd, _, _ = talib.MACD(closes.to_numpy(), fastperiod=12, slowperiod=26, signalperiod=9)
        talib_macd = macd[-1]

        assert pandas_macd == pytest.approx(talib_macd, rel=0.05)


class TestADXRegression:
    """ADX WILL differ — TA-Lib uses Wilder's smoothing, pandas used rolling."""

    def test_adx_differs_from_pandas_rolling(self):
        df = _make_price_series()
        close = df["close"].to_numpy()
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()

        talib_adx = talib.ADX(high, low, close, timeperiod=14)[-1]

        # Just verify TA-Lib produces a reasonable value
        assert 0 <= talib_adx <= 100
        # We don't assert it matches pandas — it intentionally won't


class TestATRRegression:
    """ATR WILL differ — TA-Lib uses Wilder's smoothing, pandas used rolling mean."""

    def test_atr_is_positive(self):
        df = _make_price_series()
        close = df["close"].to_numpy()
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()

        talib_atr = talib.ATR(high, low, close, timeperiod=14)[-1]
        assert talib_atr > 0


class TestScoringThresholds:
    """Verify TA-Lib outputs still produce reasonable scoring decisions.

    These thresholds come from ta_prefilter.py scoring functions.
    """

    def test_rsi_in_valid_range(self):
        df = _make_price_series()
        rsi = talib.RSI(df["close"].to_numpy(), timeperiod=14)
        valid = rsi[~np.isnan(rsi)]
        assert all(0 <= v <= 100 for v in valid)

    def test_bbw_is_positive(self):
        df = _make_price_series()
        upper, middle, lower = talib.BBANDS(df["close"].to_numpy(), timeperiod=20, nbdevup=2, nbdevdn=2)
        # bb_width = (upper - lower) / middle
        valid_mask = ~np.isnan(middle) & (middle != 0)
        bbw = (upper[valid_mask] - lower[valid_mask]) / middle[valid_mask]
        assert all(v >= 0 for v in bbw)
