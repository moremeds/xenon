"""Massive-backed historical OHLCV fetch with Massive -> DataFrame -> store contract conversion.

Contract: returns a DataFrame with columns [date, open, high, low, close, volume]
(dropping Massive's VWAP and tx_count -- Phase 2 persistence).

tz contract: Massive returns `date` as pd.Timestamp with tz="America/New_York".
The DuckDB schema in store.py declares `bar_date TIMESTAMPTZ`, so we keep tz-aware
timestamps intact all the way through write_ohlc. No tz stripping needed.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_SUPPORTED_TIMEFRAMES = {"1d", "1h"}
_STORE_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


def _today_et() -> date:
    """Return today's date in America/New_York -- avoids host-local TZ skew."""
    return datetime.now(_ET).date()


def fetch_bars(
    massive_client,
    ticker: str,
    timeframe: str,
    lookback_days: int,
    overlap_days: int = 5,
    end_date: str = "",
) -> pd.DataFrame:
    """Fetch OHLCV bars from Massive.com and reduce to the store schema.

    Args:
        massive_client: A MassiveClient instance (scripts.clients.massive_client).
        ticker: Stock symbol (e.g. "AAPL").
        timeframe: "1d" or "1h".
        lookback_days: Calendar days of history requested relative to end_date.
        overlap_days: Extra days fetched before lookback_days (incremental overlap buffer).
        end_date: End date string "YYYY-MM-DD". Empty -> today in ET.

    Returns:
        DataFrame with columns [date, open, high, low, close, volume], sorted ascending.
        `date` is tz-aware (America/New_York). Matches the store's TIMESTAMPTZ contract.

    Raises:
        ValueError: For unsupported timeframes.
    """
    if timeframe not in _SUPPORTED_TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe {timeframe!r}")

    to_dt = date.fromisoformat(end_date) if end_date else _today_et()
    from_dt = to_dt - timedelta(days=lookback_days + overlap_days)

    df = massive_client.get_aggregates(
        ticker=ticker,
        timeframe=timeframe,
        from_date=from_dt.isoformat(),
        to_date=to_dt.isoformat(),
        adjusted=True,
    )

    df = df[_STORE_COLUMNS].copy()  # drop VWAP + tx_count

    before = len(df)
    df = df[(df["open"] > 0) & (df["high"] > 0) & (df["low"] > 0) & (df["close"] > 0) & (df["volume"] >= 0)].copy()
    dropped = before - len(df)
    if dropped:
        logger.warning(
            "fetch_bars: dropped %d malformed row(s) for %s %s",
            dropped,
            ticker,
            timeframe,
        )

    df.sort_values("date", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df
