"""Unit tests for xenon.ta_lib.parquet_store."""

from __future__ import annotations

import io

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest


def _sample_ohlcv_utc() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-01-02T00:00:00Z",
                    "2026-01-03T00:00:00Z",
                    "2026-01-04T00:00:00Z",
                ],
                utc=True,
            ),
            "open": [100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0],
            "low": [99.0, 100.0, 101.0],
            "close": [100.5, 101.5, 102.5],
            "volume": [1000, 2000, 3000],
        }
    )


def _sample_indicators_utc() -> pd.DataFrame:
    ts = pd.to_datetime(["2026-01-02T00:00:00Z"], utc=True)
    row = {
        col: [0.0]
        for col in [
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
            "recent_avg_volume",
            "avg_20d_volume",
            "recent_up_ratio",
            "range_20d_pct",
            "atr_pct",
        ]
    }
    return pd.DataFrame({"timestamp": ts, **row})


def test_write_then_read_ohlcv_roundtrip():
    from xenon.ta_lib.parquet_store import read_ohlcv, write_ohlcv

    df = _sample_ohlcv_utc()
    buf = io.BytesIO()
    write_ohlcv(buf, df, timeframe="1d")
    buf.seek(0)
    got = read_ohlcv(buf)

    pd.testing.assert_frame_equal(got.reset_index(drop=True), df.reset_index(drop=True), check_dtype=False)


def test_write_enforces_utc_timezone():
    from xenon.ta_lib.parquet_store import write_ohlcv

    df = _sample_ohlcv_utc()
    buf = io.BytesIO()
    write_ohlcv(buf, df, timeframe="1d")
    buf.seek(0)
    schema = pq.read_schema(buf)
    assert str(schema.field("timestamp").type).startswith("timestamp[us, tz=UTC]")


def test_daily_bars_normalized_to_utc_midnight():
    """Amendment A9: daily bars land at 00:00:00 UTC regardless of source tz."""
    from xenon.ta_lib.parquet_store import read_ohlcv, write_ohlcv

    # Input has 04:00 UTC timestamps (i.e. midnight ET on a standard-time day)
    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-01-02T04:00:00Z", "2026-01-03T04:00:00Z"], utc=True),
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [1000, 2000],
        }
    )
    buf = io.BytesIO()
    write_ohlcv(buf, df, timeframe="1d")
    buf.seek(0)
    got = read_ohlcv(buf)
    assert got["timestamp"].iloc[0] == pd.Timestamp("2026-01-02T00:00:00Z")
    assert got["timestamp"].iloc[1] == pd.Timestamp("2026-01-03T00:00:00Z")


def test_hourly_bars_preserve_hour():
    """Hourly bars keep the true UTC hour."""
    from xenon.ta_lib.parquet_store import read_ohlcv, write_ohlcv

    df = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-01-02T14:00:00Z", "2026-01-02T15:00:00Z"], utc=True),
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [1000, 2000],
        }
    )
    buf = io.BytesIO()
    write_ohlcv(buf, df, timeframe="1h")
    buf.seek(0)
    got = read_ohlcv(buf)
    assert got["timestamp"].iloc[0].hour == 14
    assert got["timestamp"].iloc[1].hour == 15


def _write_hkt_fixture(timestamps: list[pd.Timestamp]) -> io.BytesIO:
    """Simulate the real producer: naive ts → .dt.tz_localize('Asia/Hong_Kong') → parquet.
    This matches how files actually appear in R2 (displayed wall-clock '09:30:00+0800').
    """
    hkt_series = pd.Series(timestamps).dt.tz_localize("Asia/Hong_Kong")
    n = len(timestamps)
    df = pd.DataFrame(
        {
            "timestamp": hkt_series,
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [1000] * n,
        }
    )
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), buf)
    buf.seek(0)
    return buf


def test_read_normalizes_hkt_to_utc_reinterpreting_as_et():
    """Existing bucket has tz=Asia/Hong_Kong with wall-clock values that are actually ET.
    read_ohlcv must strip HKT, localize as America/New_York, then convert to UTC."""
    from xenon.ta_lib.parquet_store import read_ohlcv

    buf = _write_hkt_fixture([pd.Timestamp("2025-11-28 09:30:00"), pd.Timestamp("2025-11-28 10:30:00")])
    df = read_ohlcv(buf)
    # "09:30" re-interpreted as ET on 2025-11-28 (EST) -> 14:30 UTC
    first_utc = df["timestamp"].iloc[0]
    assert str(first_utc.tz) == "UTC"
    assert first_utc.hour == 14 and first_utc.minute == 30


def test_read_handles_dst_spring_forward_nonexistent_time():
    """Amendment A8: 2024-03-10 02:30:00 ET doesn't exist (spring-forward gap).
    With nonexistent='shift_forward', it shifts to 03:00 ET = 07:00 UTC (EDT)."""
    from xenon.ta_lib.parquet_store import read_ohlcv

    buf = _write_hkt_fixture([pd.Timestamp("2024-03-10 02:30:00")])
    df = read_ohlcv(buf)  # must NOT raise
    first_utc = df["timestamp"].iloc[0]
    assert str(first_utc.tz) == "UTC"
    # Shifted forward to 03:00 EDT = 07:00 UTC
    assert first_utc.hour == 7 and first_utc.minute == 0


def test_read_handles_dst_fall_back_ambiguous_time():
    """Amendment A8: 2024-11-03 01:30:00 ET is ambiguous (fall-back hour).
    With ambiguous=False, ambiguous times are treated as standard time (EST),
    i.e. the second occurrence after the clock fell back. Must not raise."""
    from xenon.ta_lib.parquet_store import read_ohlcv

    buf = _write_hkt_fixture([pd.Timestamp("2024-11-03 01:30:00"), pd.Timestamp("2024-11-03 02:30:00")])
    df = read_ohlcv(buf)  # must NOT raise
    first_utc = df["timestamp"].iloc[0]
    assert str(first_utc.tz) == "UTC"
    # 01:30 treated as EST (second occurrence after fall-back) = UTC-5 -> 06:30 UTC
    assert first_utc.hour == 6


def test_read_normalizes_real_producer_file_format():
    """Regression: the real R2 producer files display wall-clock like '09:30+0800'.
    Ensure _normalize_timestamp handles that form (NOT the pyarrow-synthetic form).
    """
    from xenon.ta_lib.parquet_store import read_ohlcv

    buf = _write_hkt_fixture([pd.Timestamp("2025-11-28 09:30:00")])
    df = read_ohlcv(buf)
    got = df["timestamp"].iloc[0]
    assert str(got.tz) == "UTC"
    # 09:30 ET on 2025-11-28 (EST, offset -05:00) -> 14:30 UTC
    assert got == pd.Timestamp("2025-11-28 14:30:00", tz="UTC"), got


def test_read_drops_index_level_0_column():
    """Existing producer emits __index_level_0__ from pandas reset_index. Drop it."""
    from xenon.ta_lib.parquet_store import read_ohlcv

    tbl = pa.table(
        {
            "open": pa.array([100.0]),
            "high": pa.array([101.0]),
            "low": pa.array([99.0]),
            "close": pa.array([100.5]),
            "volume": pa.array([1000], type=pa.int64()),
            "timestamp": pa.array([pd.Timestamp("2026-01-02", tz="UTC")], type=pa.timestamp("us", tz="UTC")),
            "__index_level_0__": pa.array([0], type=pa.int64()),
        }
    )
    buf = io.BytesIO()
    pq.write_table(tbl, buf)
    buf.seek(0)
    df = read_ohlcv(buf)
    assert "__index_level_0__" not in df.columns
    assert set(df.columns) == {"timestamp", "open", "high", "low", "close", "volume"}


def test_dedupe_on_append_with_overlap():
    from xenon.ta_lib.parquet_store import dedupe_concat

    base = _sample_ohlcv_utc()
    overlap = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-01-04T00:00:00Z", "2026-01-05T00:00:00Z"], utc=True),
            "open": [999.0, 103.0],  # overlapping 2026-01-04 should prefer NEW (999.0)
            "high": [999.0, 104.0],
            "low": [999.0, 102.0],
            "close": [999.0, 103.5],
            "volume": [9999, 4000],
        }
    )
    merged = dedupe_concat(base, overlap)
    assert len(merged) == 4
    row = merged.set_index("timestamp").loc[pd.Timestamp("2026-01-04", tz="UTC")]
    assert row["open"] == 999.0


def test_write_rejects_missing_columns():
    from xenon.ta_lib.parquet_store import write_ohlcv

    bad = pd.DataFrame({"timestamp": [], "open": []})
    buf = io.BytesIO()
    with pytest.raises(ValueError, match="missing columns"):
        write_ohlcv(buf, bad, timeframe="1d")


def test_indicators_schema_roundtrip_includes_a3_fields():
    """Amendment A3: INDICATOR_COLUMNS extended with scanner-contract derived fields."""
    from xenon.ta_lib.parquet_store import (
        INDICATOR_COLUMNS,
        read_indicators,
        write_indicators,
    )

    # Verify the 5 A3 fields are present in the schema constant
    for col in (
        "recent_avg_volume",
        "avg_20d_volume",
        "recent_up_ratio",
        "range_20d_pct",
        "atr_pct",
    ):
        assert col in INDICATOR_COLUMNS, f"amendment A3: missing {col}"

    df = _sample_indicators_utc()
    buf = io.BytesIO()
    write_indicators(buf, df, timeframe="1d")
    buf.seek(0)
    got = read_indicators(buf)
    pd.testing.assert_frame_equal(got.reset_index(drop=True), df.reset_index(drop=True), check_dtype=False)
