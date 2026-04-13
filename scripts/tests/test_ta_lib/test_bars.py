"""Unit tests for ta_lib.bars with mocked IB client."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest


def _make_bar_data(n: int = 5, start_date: str = "20260401") -> list:
    """Create mock ib_insync BarData objects."""
    bars = []
    dt = datetime.strptime(start_date, "%Y%m%d")
    for i in range(n):
        bar_date = dt.replace(day=dt.day + i)
        bars.append(
            SimpleNamespace(
                date=bar_date.strftime("%Y%m%d"),
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.5 + i,
                volume=1_000_000 + i * 100_000,
            )
        )
    return bars


class TestFetchBars:
    def test_returns_dataframe(self):
        from scripts.ta_lib.bars import fetch_bars

        mock_ib = MagicMock()
        mock_ib.get_historical_data.return_value = _make_bar_data(n=5)
        mock_ib._ib = MagicMock()
        mock_ib._ib.qualifyContracts.return_value = [MagicMock()]

        result = fetch_bars(mock_ib, "AAPL", duration="1 Y", bar_size="1 day")
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 5
        assert list(result.columns) == ["date", "open", "high", "low", "close", "volume"]

    def test_date_parsed_correctly(self):
        from scripts.ta_lib.bars import fetch_bars

        mock_ib = MagicMock()
        mock_ib.get_historical_data.return_value = _make_bar_data(n=1, start_date="20260410")
        mock_ib._ib = MagicMock()
        mock_ib._ib.qualifyContracts.return_value = [MagicMock()]

        result = fetch_bars(mock_ib, "AAPL", duration="1 D", bar_size="1 day")
        assert result["date"].iloc[0] == pd.Timestamp("2026-04-10")

    def test_empty_response_raises(self):
        from scripts.ta_lib.bars import fetch_bars

        mock_ib = MagicMock()
        mock_ib.get_historical_data.return_value = []
        mock_ib._ib = MagicMock()
        mock_ib._ib.qualifyContracts.return_value = [MagicMock()]

        with pytest.raises(RuntimeError, match="No historical data"):
            fetch_bars(mock_ib, "AAPL", duration="1 Y", bar_size="1 day")

    def test_invalid_contract_raises(self):
        from scripts.ta_lib.bars import fetch_bars

        mock_ib = MagicMock()
        mock_ib._ib = MagicMock()
        mock_ib._ib.qualifyContracts.return_value = []  # qualification failed

        with pytest.raises(ValueError, match="INVALID"):
            fetch_bars(mock_ib, "INVALID", duration="1 Y", bar_size="1 day")
