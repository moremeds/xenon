"""Tests for scripts.ta_lib.bars (Massive-backed)."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from scripts.ta_lib.bars import fetch_bars


def _massive_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-04-08", tz="America/New_York"),
                "open": 170.0,
                "high": 172.0,
                "low": 169.5,
                "close": 171.5,
                "volume": 50_000_000,
                "vwap": 170.8,
                "tx_count": 450_000,
            },
            {
                "date": pd.Timestamp("2026-04-09", tz="America/New_York"),
                "open": 171.5,
                "high": 173.0,
                "low": 170.9,
                "close": 172.7,
                "volume": 48_000_000,
                "vwap": 171.9,
                "tx_count": 430_000,
            },
        ]
    )


def test_fetch_bars_returns_store_compatible_columns():
    mc = MagicMock()
    mc.get_aggregates.return_value = _massive_df()
    df = fetch_bars(mc, "AAPL", timeframe="1d", lookback_days=30)
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]


def test_fetch_bars_passes_overlap_days():
    mc = MagicMock()
    mc.get_aggregates.return_value = _massive_df()
    with patch("scripts.ta_lib.bars._today_et", return_value=date(2026, 4, 10)):
        fetch_bars(mc, "AAPL", timeframe="1d", lookback_days=30, overlap_days=5)
    kwargs = mc.get_aggregates.call_args.kwargs
    # to_date = today ET; from_date = to_date - (30 + 5) = 35 days back
    assert kwargs["to_date"] == "2026-04-10"
    assert kwargs["from_date"] == "2026-03-06"


def test_fetch_bars_1h_timeframe():
    mc = MagicMock()
    mc.get_aggregates.return_value = _massive_df()
    fetch_bars(mc, "AAPL", timeframe="1h", lookback_days=7)
    assert mc.get_aggregates.call_args.kwargs["timeframe"] == "1h"


def test_fetch_bars_filters_malformed_rows():
    bad = _massive_df().copy()
    bad.loc[len(bad)] = {
        "date": pd.Timestamp("2026-04-10", tz="America/New_York"),
        "open": 0.0,
        "high": 0.0,
        "low": 0.0,
        "close": 0.0,
        "volume": -1,
        "vwap": 0.0,
        "tx_count": 0,
    }
    mc = MagicMock()
    mc.get_aggregates.return_value = bad
    df = fetch_bars(mc, "AAPL", timeframe="1d", lookback_days=30)
    assert len(df) == 2


def test_fetch_bars_invalid_timeframe():
    with pytest.raises(ValueError):
        fetch_bars(MagicMock(), "AAPL", timeframe="5m", lookback_days=30)


def test_fetch_bars_defaults_to_ET_today():
    mc = MagicMock()
    mc.get_aggregates.return_value = _massive_df()
    with patch("scripts.ta_lib.bars._today_et", return_value=date(2026, 4, 15)):
        fetch_bars(mc, "AAPL", timeframe="1d", lookback_days=10)
    assert mc.get_aggregates.call_args.kwargs["to_date"] == "2026-04-15"
