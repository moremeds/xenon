"""Unit tests for ta_lib.store using in-memory DuckDB."""

from __future__ import annotations

from datetime import date, datetime, timezone

import duckdb
import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def conn():
    """In-memory DuckDB connection with schema initialized."""
    c = duckdb.connect(":memory:")
    from scripts.ta_lib.store import init_schema

    init_schema(c)
    yield c
    c.close()


class TestInitSchema:
    def test_tables_created(self, conn):
        tables = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()
        table_names = {t[0] for t in tables}
        assert "ohlc_bars" in table_names
        assert "ta_indicators" in table_names

    def test_idempotent(self, conn):
        from scripts.ta_lib.store import init_schema

        # Calling init_schema again should not raise
        init_schema(conn)


def _sample_ohlc_df(n: int = 5, start_date: str = "2026-04-01") -> pd.DataFrame:
    dates = pd.bdate_range(start=start_date, periods=n)
    return pd.DataFrame(
        {
            "date": dates,
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [1_000_000 + i * 100_000 for i in range(n)],
        }
    )


class TestWriteReadOhlc:
    def test_write_then_read(self, conn):
        from scripts.ta_lib.store import read_ohlc, write_ohlc

        df = _sample_ohlc_df()
        write_ohlc(conn, "AAPL", "1d", df)
        result = read_ohlc(conn, "AAPL", "1d")
        assert result is not None
        assert len(result) == 5
        assert float(result["close"].iloc[0]) == pytest.approx(100.5)

    def test_read_empty_returns_none(self, conn):
        from scripts.ta_lib.store import read_ohlc

        result = read_ohlc(conn, "NONEXISTENT", "1d")
        assert result is None

    def test_upsert_idempotent(self, conn):
        from scripts.ta_lib.store import read_ohlc, write_ohlc

        df = _sample_ohlc_df(n=3)
        write_ohlc(conn, "AAPL", "1d", df)
        # Write same data again — should not duplicate
        write_ohlc(conn, "AAPL", "1d", df)
        result = read_ohlc(conn, "AAPL", "1d")
        assert len(result) == 3

    def test_upsert_updates_values(self, conn):
        from scripts.ta_lib.store import read_ohlc, write_ohlc

        df = _sample_ohlc_df(n=1)
        write_ohlc(conn, "AAPL", "1d", df)
        # Modify close and write again
        df2 = df.copy()
        df2["close"] = 999.0
        write_ohlc(conn, "AAPL", "1d", df2)
        result = read_ohlc(conn, "AAPL", "1d")
        assert float(result["close"].iloc[0]) == pytest.approx(999.0)

    def test_null_bar_skipped(self, conn):
        from scripts.ta_lib.store import read_ohlc, write_ohlc

        df = _sample_ohlc_df(n=2)
        df.loc[0, "close"] = np.nan  # null close
        write_ohlc(conn, "AAPL", "1d", df)
        result = read_ohlc(conn, "AAPL", "1d")
        assert result is not None
        assert len(result) == 1  # only the valid row

    def test_different_tickers_isolated(self, conn):
        from scripts.ta_lib.store import read_ohlc, write_ohlc

        write_ohlc(conn, "AAPL", "1d", _sample_ohlc_df(n=3))
        write_ohlc(conn, "MSFT", "1d", _sample_ohlc_df(n=2))
        assert len(read_ohlc(conn, "AAPL", "1d")) == 3
        assert len(read_ohlc(conn, "MSFT", "1d")) == 2


class TestWriteReadIndicators:
    def test_write_then_read(self, conn):
        from scripts.ta_lib.store import read_indicators, write_indicators

        df = pd.DataFrame(
            {
                "bar_date": pd.bdate_range(start="2026-04-01", periods=3),
                "sma_20": [100.0, 101.0, 102.0],
                "sma_50": [98.0, 99.0, 100.0],
                "sma_200": [np.nan, np.nan, 95.0],
                "rsi_14": [55.0, 60.0, 65.0],
                "macd": [0.5, 0.6, 0.7],
                "macd_signal": [0.4, 0.5, 0.6],
                "macd_histogram": [0.1, 0.1, 0.1],
                "adx_14": [25.0, 28.0, 30.0],
                "bb_upper": [105.0, 106.0, 107.0],
                "bb_middle": [100.0, 101.0, 102.0],
                "bb_lower": [95.0, 96.0, 97.0],
                "bb_width": [0.1, 0.099, 0.098],
                "atr_14": [1.5, 1.6, 1.7],
            }
        )
        write_indicators(conn, "AAPL", "1d", df)
        result = read_indicators(conn, "AAPL", "1d")
        assert result is not None
        assert len(result) == 3

    def test_nullable_indicator_columns(self, conn):
        from scripts.ta_lib.store import read_indicators, write_indicators

        df = pd.DataFrame(
            {
                "bar_date": pd.bdate_range(start="2026-04-01", periods=1),
                "sma_20": [np.nan],
                "sma_50": [np.nan],
                "sma_200": [np.nan],
                "rsi_14": [np.nan],
                "macd": [np.nan],
                "macd_signal": [np.nan],
                "macd_histogram": [np.nan],
                "adx_14": [np.nan],
                "bb_upper": [np.nan],
                "bb_middle": [np.nan],
                "bb_lower": [np.nan],
                "bb_width": [np.nan],
                "atr_14": [np.nan],
            }
        )
        write_indicators(conn, "AAPL", "1d", df)
        result = read_indicators(conn, "AAPL", "1d")
        assert result is not None
        assert len(result) == 1


class TestLatestBarDate:
    def test_returns_latest(self, conn):
        from scripts.ta_lib.store import get_latest_bar_date, write_ohlc

        write_ohlc(conn, "AAPL", "1d", _sample_ohlc_df(n=5))
        latest = get_latest_bar_date(conn, "AAPL", "1d")
        assert latest is not None
        # 5 business days from 2026-04-01 → last is 2026-04-07
        assert latest == date(2026, 4, 7)

    def test_returns_none_for_missing(self, conn):
        from scripts.ta_lib.store import get_latest_bar_date

        latest = get_latest_bar_date(conn, "NONEXISTENT", "1d")
        assert latest is None
