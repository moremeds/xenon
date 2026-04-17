"""TAService — read-through view over the local Apex R2 parquet mirror.

Scanner-side consumer API. No DuckDB, no Massive client, no cache refresh
logic — those responsibilities live in the GitHub Action (apex_refresh) and
the apex_sync module.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.ta_lib.parquet_store import read_indicators, read_ohlcv

logger = logging.getLogger(__name__)

# Rename map: parquet indicator column → scanner-expected key
_FIELD_MAP: dict[str, str] = {
    "sma_20": "ma_20",
    "sma_50": "ma_50",
    "sma_200": "ma_200",
    "rsi_14": "rsi",
    "adx_14": "adx",
    "bb_width": "bbw",
}

# Keys that pass through unchanged (already scanner-friendly)
_PASSTHROUGH_INDICATOR_KEYS: tuple[str, ...] = (
    "macd",
    "macd_signal",
    "macd_histogram",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "atr_14",
    "high_20d",
    "low_20d",
    "high_52w",
    "low_52w",
    "up_day_volume_ratio",
    "recent_avg_volume",
    "avg_20d_volume",
    "recent_up_ratio",
    "range_20d_pct",
    "atr_pct",
)


class TAService:
    """Read-only service backed by local parquet mirror."""

    def __init__(self, mirror_dir: Path | str = "data/apex_mirror"):
        self._mirror = Path(mirror_dir)

    def _hist_path(self, ticker: str, timeframe: str) -> Path:
        return self._mirror / "parquet" / "historical" / timeframe / f"{ticker}.parquet"

    def _ind_path(self, ticker: str, timeframe: str) -> Path:
        return self._mirror / "parquet" / "indicators" / timeframe / f"{ticker}.parquet"

    def get_ohlcv(self, ticker: str, timeframe: str = "1d") -> pd.DataFrame | None:
        path = self._hist_path(ticker, timeframe)
        if not path.exists():
            return None
        return read_ohlcv(str(path))

    def get_indicators(self, ticker: str, timeframe: str = "1d") -> pd.DataFrame | None:
        path = self._ind_path(ticker, timeframe)
        if not path.exists():
            return None
        return read_indicators(str(path))

    def get_snapshot(self, ticker: str, timeframe: str = "1d") -> dict[str, Any] | None:
        """Return the latest-row scanner snapshot merging OHLCV + indicators.

        Keys produced (preserving the contract in test_snapshot_contract.py):
          ticker, close, open, high, low, volume, price, dollar_volume,
          ma_20, ma_50, ma_200, rsi, adx, bbw,
          macd, macd_signal, macd_histogram,
          bb_upper, bb_middle, bb_lower, atr_14,
          high_20d, low_20d, high_52w, low_52w,
          up_day_volume_ratio, recent_avg_volume, avg_20d_volume,
          recent_up_ratio, range_20d_pct, atr_pct,
          ma_20_series (last 5 non-NaN sma_20 values).

        Returns None when either parquet is missing or empty.
        """
        ohlcv = self.get_ohlcv(ticker, timeframe)
        ind = self.get_indicators(ticker, timeframe)
        if ohlcv is None or len(ohlcv) == 0 or ind is None or len(ind) == 0:
            return None

        last_ohlcv = ohlcv.iloc[-1]
        last_ind = ind.iloc[-1]
        close = float(last_ohlcv["close"]) if pd.notna(last_ohlcv["close"]) else 0.0
        volume = float(last_ohlcv["volume"]) if pd.notna(last_ohlcv["volume"]) else 0.0

        snap: dict[str, Any] = {
            "ticker": ticker,
            "timeframe": timeframe,
            "close": close,
            "open": float(last_ohlcv.get("open", 0.0) or 0.0),
            "high": float(last_ohlcv.get("high", 0.0) or 0.0),
            "low": float(last_ohlcv.get("low", 0.0) or 0.0),
            "volume": volume,
            "dollar_volume": close * volume,
            "price": close,
        }

        # Remap renamed fields
        for src, dest in _FIELD_MAP.items():
            value = last_ind.get(src)
            snap[dest] = _coerce_float(value)

        # Passthrough fields
        for col in _PASSTHROUGH_INDICATOR_KEYS:
            value = last_ind.get(col)
            snap[col] = _coerce_float(value)

        # ma_20_series: last 5 non-NaN sma_20 values (oldest → newest)
        sma_20 = ind["sma_20"].dropna().tail(5).tolist()
        snap["ma_20_series"] = [float(v) for v in sma_20]

        return snap


def _coerce_float(value: Any) -> float:
    """NaN / None / non-numeric → 0.0. Matches the old TAService's coercion at the boundary."""
    try:
        if value is None or pd.isna(value):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
