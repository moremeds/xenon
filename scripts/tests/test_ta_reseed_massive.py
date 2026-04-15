"""Smoke test for ta_reseed_massive — mocked Massive, real DuckDB."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

_INDICATOR_COLS = [
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


def _mock_df():
    return pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-04-08", tz="America/New_York"),
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.05,
                "volume": 1000,
                "vwap": 1.0,
                "tx_count": 10,
            }
        ]
    )


def _mock_indicators_df():
    df = _mock_df().assign(bar_date=lambda d: d["date"])
    for col in _INDICATOR_COLS:
        df[col] = 0.0
    return df


def test_reseed_loops_timeframes_and_uses_lookback(tmp_path):
    mc = MagicMock()
    mc.get_aggregates.return_value = _mock_df()

    with (
        patch("scripts.ta_reseed_massive.MassiveClient", return_value=mc),
        patch("scripts.ta_reseed_massive.compute_all", return_value=_mock_indicators_df()),
    ):
        import scripts.ta_reseed_massive as reseed

        exit_code = reseed.main(
            [
                "--db",
                str(tmp_path / "ta.duckdb"),
                "--tickers",
                "AAPL,SPY",
                "--timeframes",
                "1d,1h",
                "--lookback-days",
                "30",
            ]
        )
    assert exit_code == 0
    # 2 tickers × 2 timeframes = 4 fetches
    assert mc.get_aggregates.call_count == 4
    # --lookback-days must flow through
    for call in mc.get_aggregates.call_args_list:
        assert "from_date" in call.kwargs
