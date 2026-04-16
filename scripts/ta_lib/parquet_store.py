"""Parquet I/O for OHLCV and indicator snapshots.

Writers emit timestamp[us, tz=UTC]. Daily bars are normalized to UTC-midnight.
Readers normalize the legacy tz=Asia/Hong_Kong label (producer bug: wall-clock
values are actually America/New_York) by stripping the tz, re-localizing to ET,
and converting to UTC. DST transitions are handled by passing
ambiguous=False, nonexistent='shift_forward' (see _normalize_timestamp for
why False rather than 'infer').

The indicator schema is extended beyond the raw TA-Lib outputs to include the
derived fields that the scanner's snapshot contract requires
(recent_avg_volume, avg_20d_volume, recent_up_ratio, range_20d_pct, atr_pct).
"""

from __future__ import annotations

from typing import IO

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

OHLCV_COLUMNS: tuple[str, ...] = ("timestamp", "open", "high", "low", "close", "volume")

INDICATOR_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "sma_20",
    "sma_50",
    "sma_200",
    "rsi_14",
    "adx_14",
    "macd",
    "macd_signal",
    "macd_histogram",
    "bb_upper",
    "bb_middle",
    "bb_lower",
    "bb_width",
    "atr_14",
    "high_20d",
    "low_20d",
    "high_52w",
    "low_52w",
    "up_day_volume_ratio",
    # Added per amendment A3 to preserve scanner snapshot contract
    "recent_avg_volume",  # volume.rolling(5).mean() — last-5-bar average
    "avg_20d_volume",  # volume.rolling(20).mean()
    "recent_up_ratio",  # fraction of up-days in trailing 20
    "range_20d_pct",  # (high.rolling(20).max() - low.rolling(20).min()) / close
    "atr_pct",  # atr_14 / close
)

_ET = "America/New_York"
_UTC = "UTC"


def _validate_columns(df: pd.DataFrame, required: tuple[str, ...]) -> None:
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"missing columns: {missing}")


def _normalize_timestamp(series: pd.Series) -> pd.Series:
    """Return series with UTC-aware timestamps.

    The legacy producer wrote parquet files with tz=Asia/Hong_Kong where the
    displayed wall-clock (e.g. '09:30:00+0800') is actually meant to be the ET
    wall-clock. To recover: strip the wrong tz label (keeps the wall-clock
    digits), re-localize as America/New_York, convert to UTC.

    DST-safe: ambiguous=False treats fall-back 01:00-02:00 ET as standard time
    (the second clock occurrence). nonexistent='shift_forward' maps spring-forward
    gap 02:00-03:00 ET to the first valid post-transition instant.
    """
    if series.dt.tz is None:
        return series.dt.tz_localize(_ET, ambiguous=False, nonexistent="shift_forward").dt.tz_convert(_UTC)
    if str(series.dt.tz) != _UTC:
        return (
            series.dt.tz_localize(None)
            .dt.tz_localize(_ET, ambiguous=False, nonexistent="shift_forward")
            .dt.tz_convert(_UTC)
        )
    return series


def _to_utc_us_table(
    df: pd.DataFrame,
    columns: tuple[str, ...],
    timeframe: str,
) -> pa.Table:
    out = df.loc[:, list(columns)].copy()
    ts = _normalize_timestamp(pd.to_datetime(out["timestamp"]))
    if timeframe == "1d":
        ts = ts.dt.normalize()  # -> YYYY-MM-DD 00:00:00+00:00
    out["timestamp"] = ts
    schema_fields = []
    for col in columns:
        if col == "timestamp":
            schema_fields.append(pa.field(col, pa.timestamp("us", tz="UTC")))
        elif col == "volume":
            schema_fields.append(pa.field(col, pa.int64()))
        else:
            schema_fields.append(pa.field(col, pa.float64()))
    return pa.Table.from_pandas(out, schema=pa.schema(schema_fields), preserve_index=False)


def write_ohlcv(sink: IO[bytes] | str, df: pd.DataFrame, timeframe: str = "1h") -> None:
    _validate_columns(df, OHLCV_COLUMNS)
    pq.write_table(_to_utc_us_table(df, OHLCV_COLUMNS, timeframe), sink)


def write_indicators(sink: IO[bytes] | str, df: pd.DataFrame, timeframe: str = "1h") -> None:
    _validate_columns(df, INDICATOR_COLUMNS)
    pq.write_table(_to_utc_us_table(df, INDICATOR_COLUMNS, timeframe), sink)


def _read_and_normalize(source: IO[bytes] | str, columns: tuple[str, ...]) -> pd.DataFrame:
    df = pq.read_table(source).to_pandas()
    df = df.drop(columns=["__index_level_0__"], errors="ignore")
    df["timestamp"] = _normalize_timestamp(pd.to_datetime(df["timestamp"]))
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(f"parquet missing required columns: {missing}")
    return df.loc[:, list(columns)].reset_index(drop=True)


def read_ohlcv(source: IO[bytes] | str) -> pd.DataFrame:
    return _read_and_normalize(source, OHLCV_COLUMNS)


def read_indicators(source: IO[bytes] | str) -> pd.DataFrame:
    return _read_and_normalize(source, INDICATOR_COLUMNS)


def dedupe_concat(base: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    combined = pd.concat([base, new], ignore_index=True)
    combined = combined.drop_duplicates(subset=["timestamp"], keep="last")
    return combined.sort_values("timestamp").reset_index(drop=True)
