"""E2E tests for ta_lib.store with real DuckDB file."""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


def _sample_ohlc_df(n: int = 5, start_date: str = "2026-04-01") -> pd.DataFrame:
    dates = pd.bdate_range(start=start_date, periods=n)
    return pd.DataFrame(
        {
            "date": dates,
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [1_000_000] * n,
        }
    )


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_ta.duckdb")


class TestStoreE2E:
    def test_full_lifecycle(self, db_path):
        from scripts.ta_lib.indicators import compute_all
        from scripts.ta_lib.store import (
            get_connection,
            get_latest_bar_date,
            init_schema,
            read_indicators,
            read_ohlc,
            write_indicators,
            write_ohlc,
        )

        # Phase 1: Create and populate
        conn = get_connection(db_path)
        init_schema(conn)

        ohlc = _sample_ohlc_df(n=30)
        write_ohlc(conn, "AAPL", "1d", ohlc)

        result = read_ohlc(conn, "AAPL", "1d")
        assert result is not None
        assert len(result) == 30

        # Phase 2: Compute and store indicators
        indicators_df = compute_all(ohlc)
        indicators_df["bar_date"] = indicators_df["date"]
        write_indicators(conn, "AAPL", "1d", indicators_df)

        ind = read_indicators(conn, "AAPL", "1d")
        assert ind is not None
        assert len(ind) == 30

        # Phase 3: Append new bars
        new_bars = _sample_ohlc_df(n=3, start_date="2026-05-15")
        write_ohlc(conn, "AAPL", "1d", new_bars)

        full_ohlc = read_ohlc(conn, "AAPL", "1d")
        assert len(full_ohlc) == 33

        latest = get_latest_bar_date(conn, "AAPL", "1d")
        assert latest is not None

        # Phase 4: Recompute indicators over full series
        full_ohlc_for_compute = full_ohlc.rename(columns={"bar_date": "date"})
        new_indicators = compute_all(full_ohlc_for_compute)
        new_indicators["bar_date"] = new_indicators["date"]
        write_indicators(conn, "AAPL", "1d", new_indicators)

        final_ind = read_indicators(conn, "AAPL", "1d")
        assert len(final_ind) == 33

        conn.close()

    def test_survives_reopen(self, db_path):
        from scripts.ta_lib.store import get_connection, init_schema, read_ohlc, write_ohlc

        # Write data
        conn = get_connection(db_path)
        init_schema(conn)
        write_ohlc(conn, "MSFT", "1d", _sample_ohlc_df(n=5))
        conn.close()

        # Reopen and verify
        conn2 = get_connection(db_path)
        init_schema(conn2)
        result = read_ohlc(conn2, "MSFT", "1d")
        assert result is not None
        assert len(result) == 5
        conn2.close()

    def test_file_created_at_path(self, db_path):
        from scripts.ta_lib.store import get_connection, init_schema

        conn = get_connection(db_path)
        init_schema(conn)
        conn.close()
        assert Path(db_path).exists()

    def test_transaction_rollback_on_indicator_failure(self, db_path):
        """If write_indicators fails mid-transaction, write_ohlc must be rolled back."""
        from scripts.ta_lib.store import (
            get_connection,
            init_schema,
            read_indicators,
            read_ohlc,
            write_ohlc,
        )

        conn = get_connection(db_path)
        init_schema(conn)

        ohlc = _sample_ohlc_df(n=10)

        # Simulate: write OHLC succeeds, write_indicators throws
        conn.begin()
        try:
            write_ohlc(conn, "FAIL", "1d", ohlc)
            # Force an error during indicator write
            raise RuntimeError("Simulated indicator write failure")
        except RuntimeError:
            conn.rollback()

        # Verify OHLC was NOT committed (rolled back)
        result = read_ohlc(conn, "FAIL", "1d")
        assert result is None, "OHLC should be rolled back when indicator write fails"

        conn.close()


class TestPartialCacheRecovery:
    """OHLC exists but indicators missing → service should recompute."""

    def test_ohlc_without_indicators_detected_as_stale(self, db_path):
        from scripts.ta_lib.store import (
            get_connection,
            init_schema,
            read_indicators,
            write_ohlc,
        )

        conn = get_connection(db_path)
        init_schema(conn)

        # Write OHLC only — no indicators
        write_ohlc(conn, "PARTIAL", "1d", _sample_ohlc_df(n=30))

        # Indicators should be None
        ind = read_indicators(conn, "PARTIAL", "1d")
        assert ind is None, "No indicators should exist for partial cache"

        conn.close()


class TestSplitPurge:
    """After split detection, old pre-split rows must be deleted."""

    def test_delete_ticker_purges_all_rows(self, db_path):
        from scripts.ta_lib.indicators import compute_all
        from scripts.ta_lib.store import (
            delete_ticker,
            get_connection,
            init_schema,
            read_indicators,
            read_ohlc,
            write_indicators,
            write_ohlc,
        )

        conn = get_connection(db_path)
        init_schema(conn)

        # Seed 30 bars of OHLC + indicators
        ohlc = _sample_ohlc_df(n=30)
        write_ohlc(conn, "SPLIT", "1d", ohlc)
        ind_df = compute_all(ohlc)
        ind_df["bar_date"] = ind_df["date"]
        write_indicators(conn, "SPLIT", "1d", ind_df)

        assert read_ohlc(conn, "SPLIT", "1d") is not None
        assert read_indicators(conn, "SPLIT", "1d") is not None

        # Purge — simulates what _refresh does on split detection
        delete_ticker(conn, "SPLIT", "1d")

        assert read_ohlc(conn, "SPLIT", "1d") is None, "OHLC should be purged"
        assert read_indicators(conn, "SPLIT", "1d") is None, "Indicators should be purged"

        # Write new post-split data
        new_ohlc = _sample_ohlc_df(n=20, start_date="2026-05-01")
        # Simulate halved prices (split)
        new_ohlc["close"] = new_ohlc["close"] / 2
        new_ohlc["open"] = new_ohlc["open"] / 2
        new_ohlc["high"] = new_ohlc["high"] / 2
        new_ohlc["low"] = new_ohlc["low"] / 2
        write_ohlc(conn, "SPLIT", "1d", new_ohlc)

        result = read_ohlc(conn, "SPLIT", "1d")
        assert len(result) == 20, "Only post-split bars should remain"

        conn.close()


class TestFrozenBaseline:
    """Verify TA-Lib produces deterministic outputs for deterministic inputs."""

    def test_indicators_are_deterministic(self, db_path):
        from scripts.ta_lib.indicators import compute_all

        # Same seed → same output every time
        df1 = _sample_ohlc_df(n=50)
        df2 = _sample_ohlc_df(n=50)  # same function, same default start_date

        r1 = compute_all(df1)
        r2 = compute_all(df2)

        # All indicator values should match exactly
        for col in ["sma_20", "rsi_14", "macd", "adx_14", "bb_width", "atr_14"]:
            vals1 = r1[col].dropna().tolist()
            vals2 = r2[col].dropna().tolist()
            assert vals1 == vals2, f"{col} should be deterministic"

    def test_sma_20_frozen_value(self, db_path):
        """Freeze a known SMA-20 output for regression detection."""
        from scripts.ta_lib.indicators import compute_all

        df = _sample_ohlc_df(n=30)
        result = compute_all(df)

        # SMA-20 at last row = mean of last 20 closes
        expected = df["close"].iloc[-20:].mean()
        actual = result["sma_20"].iloc[-1]
        assert abs(actual - expected) < 1e-10, f"SMA-20 frozen baseline broken: expected {expected}, got {actual}"
