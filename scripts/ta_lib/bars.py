"""Producer-side Massive → OHLCV DataFrame adapter.

Called exclusively by scripts.apex_refresh (the GitHub Action). Translates
MassiveClient.get_aggregates() output (columns: date, open, high, low, close,
volume, vwap, tx_count with tz-aware ET timestamps) into the canonical OHLCV
shape expected by parquet_store.write_ohlcv (columns: timestamp, open, high,
low, close, volume — tz-aware input is accepted; parquet_store handles
normalization to UTC).
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from xenon.clients.massive_client import MassiveClient
from scripts.ta_lib.parquet_store import OHLCV_COLUMNS


def fetch_bars(
    client: MassiveClient,
    ticker: str,
    *,
    timeframe: str,
    start: date,
    end: date,
) -> pd.DataFrame:
    """Fetch bars for `ticker` in `timeframe` from `start` through `end` (inclusive).

    Raises MassiveError subclasses on vendor failure; callers are responsible
    for catching and translating to RefreshResult.
    """
    raw = client.get_aggregates(ticker, timeframe, start.isoformat(), end.isoformat())
    renamed = raw.rename(columns={"date": "timestamp"})
    return renamed.loc[:, list(OHLCV_COLUMNS)].reset_index(drop=True)
