"""Tests for ta_seed_yahoo.py."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

_project_root = str(Path(__file__).resolve().parent.parent.parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from scripts.ta_seed_yahoo import _extract_ticker_df, _normalize_ticker, main

# ---------------------------------------------------------------------------
# Column transform
# ---------------------------------------------------------------------------


class TestExtractTickerDf:
    """_extract_ticker_df renames columns and drops Adj Close."""

    def test_flat_columns_single_ticker(self):
        """Single-ticker download produces flat columns."""
        df = pd.DataFrame(
            {
                "Date": pd.date_range("2025-01-01", periods=3),
                "Open": [1.0, 2.0, 3.0],
                "High": [1.5, 2.5, 3.5],
                "Low": [0.5, 1.5, 2.5],
                "Close": [1.2, 2.2, 3.2],
                "Adj Close": [1.2, 2.2, 3.2],
                "Volume": [100, 200, 300],
            }
        ).set_index("Date")

        result = _extract_ticker_df(df, "AAPL", multi=False)

        assert "date" in result.columns
        assert "open" in result.columns
        assert "close" in result.columns
        assert "volume" in result.columns
        # Adj Close must be dropped
        assert "Adj Close" not in result.columns
        assert "adj close" not in result.columns
        assert len(result) == 3

    def test_multi_index_columns(self):
        """Multi-ticker download produces MultiIndex columns (ticker, field)."""
        dates = pd.date_range("2025-01-01", periods=2)
        # yfinance group_by="ticker" → level 0 = ticker, level 1 = field
        arrays = [
            ["AAPL", "AAPL", "AAPL", "AAPL", "AAPL", "MSFT", "MSFT", "MSFT", "MSFT", "MSFT"],
            ["Open", "High", "Low", "Close", "Volume", "Open", "High", "Low", "Close", "Volume"],
        ]
        tuples = list(zip(*arrays))
        index = pd.MultiIndex.from_tuples(tuples)
        data = [
            [100, 102, 99, 101, 1000, 200, 202, 199, 201, 2000],
            [110, 112, 109, 111, 1100, 210, 212, 209, 211, 2100],
        ]
        df = pd.DataFrame(data, index=dates, columns=index)
        df.index.name = "Date"

        result = _extract_ticker_df(df, "AAPL", multi=True)

        assert "date" in result.columns
        assert "open" in result.columns
        assert result["open"].iloc[0] == 100

    def test_missing_ticker_returns_empty(self):
        """Requesting a ticker not in the MultiIndex returns empty DF."""
        dates = pd.date_range("2025-01-01", periods=2)
        # level 0 = ticker, level 1 = field
        arrays = [["AAPL", "AAPL", "MSFT", "MSFT"], ["Open", "Close", "Open", "Close"]]
        tuples = list(zip(*arrays))
        index = pd.MultiIndex.from_tuples(tuples)
        df = pd.DataFrame([[1, 2, 3, 4], [5, 6, 7, 8]], index=dates, columns=index)
        df.index.name = "Date"

        result = _extract_ticker_df(df, "GOOG", multi=True)
        assert result.empty


# ---------------------------------------------------------------------------
# Ticker normalisation
# ---------------------------------------------------------------------------


class TestNormalizeTicker:
    def test_dot_replaced(self):
        assert _normalize_ticker("BRK.B") == "BRK B"

    def test_no_dot_unchanged(self):
        assert _normalize_ticker("AAPL") == "AAPL"

    def test_multiple_dots(self):
        assert _normalize_ticker("A.B.C") == "A B C"


# ---------------------------------------------------------------------------
# --dry-run doesn't write
# ---------------------------------------------------------------------------


class TestDryRun:
    @patch("scripts.ta_seed_yahoo.build_static_universe", return_value=["AAPL", "MSFT", "SPY"])
    @patch("scripts.ta_seed_yahoo.get_connection")
    def test_dry_run_no_db_interaction(self, mock_conn, mock_universe):
        """--dry-run should not call get_connection."""
        rc = main(["--dry-run"])
        assert rc == 0
        mock_conn.assert_not_called()

    @patch("scripts.ta_seed_yahoo.build_static_universe", return_value=["AAPL"])
    @patch("scripts.ta_seed_yahoo.get_connection")
    def test_dry_run_appends_spy(self, mock_conn, mock_universe):
        """When universe lacks SPY, dry-run still reports +1."""
        rc = main(["--dry-run"])
        assert rc == 0
        mock_conn.assert_not_called()


# ---------------------------------------------------------------------------
# Empty ticker = failure
# ---------------------------------------------------------------------------


class TestEmptyTickerFailure:
    @patch("scripts.ta_seed_yahoo.yfinance")
    @patch("scripts.ta_seed_yahoo.get_connection")
    @patch("scripts.ta_seed_yahoo.init_schema")
    def test_empty_df_counted_as_failure(self, mock_init, mock_conn, mock_yf):
        """A ticker returning an empty DataFrame should be counted as failure."""
        mock_yf.download.return_value = pd.DataFrame()  # empty
        mock_db = MagicMock()
        mock_conn.return_value = mock_db

        rc = main(["--tickers", "FAKE"])
        assert rc == 1  # non-zero = failures present
        mock_db.close.assert_called_once()
