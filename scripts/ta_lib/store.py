"""DuckDB storage layer for OHLC bars and TA indicators."""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import duckdb
import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "data/ta.duckdb"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ohlc_bars (
    ticker     VARCHAR NOT NULL,
    timeframe  VARCHAR NOT NULL,
    bar_date   TIMESTAMPTZ NOT NULL,
    open       DOUBLE NOT NULL,
    high       DOUBLE NOT NULL,
    low        DOUBLE NOT NULL,
    close      DOUBLE NOT NULL,
    volume     BIGINT NOT NULL,
    fetched_at TIMESTAMPTZ DEFAULT current_timestamp,
    PRIMARY KEY (ticker, timeframe, bar_date)
);

CREATE TABLE IF NOT EXISTS ta_indicators (
    ticker          VARCHAR NOT NULL,
    timeframe       VARCHAR NOT NULL,
    bar_date        TIMESTAMPTZ NOT NULL,
    sma_20          DOUBLE,
    sma_50          DOUBLE,
    sma_200         DOUBLE,
    rsi_14          DOUBLE,
    macd            DOUBLE,
    macd_signal     DOUBLE,
    macd_histogram  DOUBLE,
    adx_14          DOUBLE,
    bb_upper        DOUBLE,
    bb_middle       DOUBLE,
    bb_lower        DOUBLE,
    bb_width        DOUBLE,
    atr_14          DOUBLE,
    computed_at     TIMESTAMPTZ DEFAULT current_timestamp,
    PRIMARY KEY (ticker, timeframe, bar_date)
);
"""


def get_connection(db_path: str = DEFAULT_DB_PATH) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection, creating parent dirs if needed."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(db_path)


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create tables if they don't exist."""
    conn.execute(SCHEMA_SQL)


def read_ohlc(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    timeframe: str,
) -> Optional[pd.DataFrame]:
    """Read all OHLC bars for a ticker/timeframe. Returns None if no rows."""
    df = conn.execute(
        "SELECT bar_date, open, high, low, close, volume FROM ohlc_bars "
        "WHERE ticker = ? AND timeframe = ? ORDER BY bar_date",
        [ticker, timeframe],
    ).fetchdf()
    return df if len(df) > 0 else None


def write_ohlc(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    timeframe: str,
    df: pd.DataFrame,
) -> None:
    """UPSERT OHLC bars. df must have columns: date, open, high, low, close, volume."""
    # Filter out rows with null OHLC values
    required = ["open", "high", "low", "close", "volume"]
    clean = df.dropna(subset=required).copy()
    if len(clean) < len(df):
        dropped = len(df) - len(clean)
        logger.warning("Skipping %d bars with null OHLC for %s", dropped, ticker)
    if clean.empty:
        return

    # Prepare staging DataFrame with ticker/timeframe columns
    staging = clean[["date", "open", "high", "low", "close", "volume"]].copy()
    staging = staging.rename(columns={"date": "bar_date"})
    staging["ticker"] = ticker
    staging["timeframe"] = timeframe
    staging["open"] = staging["open"].astype(float)
    staging["high"] = staging["high"].astype(float)
    staging["low"] = staging["low"].astype(float)
    staging["close"] = staging["close"].astype(float)
    staging["volume"] = staging["volume"].astype(int)

    conn.register("_staging_ohlc", staging)
    conn.execute(
        "INSERT INTO ohlc_bars (ticker, timeframe, bar_date, open, high, low, close, volume) "
        "SELECT ticker, timeframe, bar_date, open, high, low, close, volume FROM _staging_ohlc "
        "ON CONFLICT (ticker, timeframe, bar_date) DO UPDATE SET "
        "open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, "
        "close=EXCLUDED.close, volume=EXCLUDED.volume, fetched_at=now()"
    )
    conn.unregister("_staging_ohlc")


def read_indicators(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    timeframe: str,
) -> Optional[pd.DataFrame]:
    """Read all indicator rows for a ticker/timeframe. Returns None if no rows."""
    df = conn.execute(
        "SELECT * FROM ta_indicators WHERE ticker = ? AND timeframe = ? ORDER BY bar_date",
        [ticker, timeframe],
    ).fetchdf()
    return df if len(df) > 0 else None


def write_indicators(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    timeframe: str,
    df: pd.DataFrame,
) -> None:
    """UPSERT indicator rows using DuckDB bulk API."""
    indicator_cols = [
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
    ]
    staging = df[["bar_date"] + indicator_cols].copy()
    staging["ticker"] = ticker
    staging["timeframe"] = timeframe

    conn.register("_staging_ind", staging)
    col_list = ", ".join(indicator_cols)
    update_list = ", ".join(f"{c}=EXCLUDED.{c}" for c in indicator_cols)
    conn.execute(
        f"INSERT INTO ta_indicators (ticker, timeframe, bar_date, {col_list}) "
        f"SELECT ticker, timeframe, bar_date, {col_list} FROM _staging_ind "
        f"ON CONFLICT (ticker, timeframe, bar_date) DO UPDATE SET "
        f"{update_list}, computed_at=now()"
    )
    conn.unregister("_staging_ind")


def delete_ticker(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    timeframe: str,
) -> None:
    """Delete all OHLC and indicator rows for a ticker/timeframe (used for split re-fetch)."""
    conn.execute("DELETE FROM ta_indicators WHERE ticker = ? AND timeframe = ?", [ticker, timeframe])
    conn.execute("DELETE FROM ohlc_bars WHERE ticker = ? AND timeframe = ?", [ticker, timeframe])


def get_latest_bar_date(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    timeframe: str,
) -> Optional[date]:
    """Return the most recent bar_date for a ticker/timeframe, or None."""
    result = conn.execute(
        "SELECT MAX(bar_date) FROM ohlc_bars WHERE ticker = ? AND timeframe = ?",
        [ticker, timeframe],
    ).fetchone()
    if result and result[0] is not None:
        val = result[0]
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, date):
            return val
        return pd.Timestamp(val).date()
    return None


def get_latest_bar_timestamp(
    conn: duckdb.DuckDBPyConnection,
    ticker: str,
    timeframe: str,
) -> Optional[datetime]:
    """Return the most recent bar_date as a full timestamp (for intraday freshness)."""
    result = conn.execute(
        "SELECT MAX(bar_date) FROM ohlc_bars WHERE ticker = ? AND timeframe = ?",
        [ticker, timeframe],
    ).fetchone()
    if result and result[0] is not None:
        val = result[0]
        if isinstance(val, datetime):
            return val
        return pd.Timestamp(val).to_pydatetime()
    return None
