"""TA-Lib indicator computation with post-processing."""

from __future__ import annotations

import numpy as np
import pandas as pd
import talib


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """Run all TA indicators on an OHLCV DataFrame.

    Args:
        df: DataFrame with columns [open, high, low, close, volume].
            Must be sorted by date ascending.

    Returns:
        Copy of df with indicator columns appended.
    """
    result = df.copy()
    close = result["close"].to_numpy(dtype=np.float64)
    high = result["high"].to_numpy(dtype=np.float64)
    low = result["low"].to_numpy(dtype=np.float64)

    # Moving averages
    result["sma_20"] = talib.SMA(close, timeperiod=20)
    result["sma_50"] = talib.SMA(close, timeperiod=50)
    result["sma_200"] = talib.SMA(close, timeperiod=200)

    # RSI
    rsi = talib.RSI(close, timeperiod=14)
    # Post-process: coerce flat-from-inception to 50.0
    # TA-Lib returns 0.0 for series with zero gains (both truly-flat and
    # flat-after-drop). Only coerce when the ENTIRE history up to that point
    # has zero variance — meaning it's been flat from the start, not flat
    # after a prior move (where Wilder's smoothing correctly remembers the loss).
    for i in range(14, len(rsi)):
        if np.isnan(rsi[i]) or rsi[i] == 0.0:
            # Check full history from start to current bar (not just local window)
            if np.std(close[: i + 1]) < 1e-10:
                rsi[i] = 50.0
    result["rsi_14"] = rsi

    # MACD
    macd, macd_signal, macd_hist = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
    result["macd"] = macd
    result["macd_signal"] = macd_signal
    result["macd_histogram"] = macd_hist

    # ADX (Wilder's smoothing — intentionally different from old pandas rolling)
    result["adx_14"] = talib.ADX(high, low, close, timeperiod=14)

    # Bollinger Bands
    bb_upper, bb_middle, bb_lower = talib.BBANDS(close, timeperiod=20, nbdevup=2, nbdevdn=2, matype=0)
    result["bb_upper"] = bb_upper
    result["bb_middle"] = bb_middle
    result["bb_lower"] = bb_lower
    # Derived: bb_width = (upper - lower) / middle
    # Preserve NaN for warmup rows per spec — don't coerce to 0.0
    with np.errstate(divide="ignore", invalid="ignore"):
        bb_width = (bb_upper - bb_lower) / bb_middle
    # Only fix divide-by-zero (middle=0) to NaN, leave warmup NaN as-is
    result["bb_width"] = np.where(np.isinf(bb_width), np.nan, bb_width)

    # ATR (Wilder's smoothing)
    result["atr_14"] = talib.ATR(high, low, close, timeperiod=14)

    return result
