"""Tests for scripts.ta_lib.bars — producer-side Massive→OHLCV adapter."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pandas as pd
import pytest


def _massive_aggregates_response() -> pd.DataFrame:
    """Shape of what MassiveClient.get_aggregates returns."""
    return pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-01-02T09:30:00-05:00", "2026-01-02T10:30:00-05:00"]),
            "open": [100.0, 101.0],
            "high": [101.0, 102.0],
            "low": [99.0, 100.0],
            "close": [100.5, 101.5],
            "volume": [1000, 2000],
            "vwap": [100.25, 101.25],
            "tx_count": [150, 175],
        }
    )


def test_fetch_bars_calls_get_aggregates_with_iso_dates():
    from scripts.ta_lib.bars import fetch_bars

    client = MagicMock()
    client.get_aggregates.return_value = _massive_aggregates_response()

    fetch_bars(client, "AAPL", timeframe="1h", start=date(2026, 1, 1), end=date(2026, 1, 2))

    client.get_aggregates.assert_called_once_with("AAPL", "1h", "2026-01-01", "2026-01-02")


def test_fetch_bars_renames_date_to_timestamp():
    from scripts.ta_lib.bars import fetch_bars

    client = MagicMock()
    client.get_aggregates.return_value = _massive_aggregates_response()

    df = fetch_bars(client, "AAPL", timeframe="1h", start=date(2026, 1, 1), end=date(2026, 1, 2))

    assert "timestamp" in df.columns
    assert "date" not in df.columns


def test_fetch_bars_drops_vwap_and_tx_count():
    from scripts.ta_lib.bars import fetch_bars

    client = MagicMock()
    client.get_aggregates.return_value = _massive_aggregates_response()

    df = fetch_bars(client, "AAPL", timeframe="1h", start=date(2026, 1, 1), end=date(2026, 1, 2))

    assert set(df.columns) == {"timestamp", "open", "high", "low", "close", "volume"}


def test_fetch_bars_returns_columns_in_canonical_order():
    from scripts.ta_lib.bars import fetch_bars

    client = MagicMock()
    client.get_aggregates.return_value = _massive_aggregates_response()

    df = fetch_bars(client, "AAPL", timeframe="1h", start=date(2026, 1, 1), end=date(2026, 1, 2))

    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]


def test_fetch_bars_preserves_tz_aware_timestamps():
    """The MassiveClient returns ET-aware timestamps. Adapter must not strip the tz."""
    from scripts.ta_lib.bars import fetch_bars

    client = MagicMock()
    client.get_aggregates.return_value = _massive_aggregates_response()

    df = fetch_bars(client, "AAPL", timeframe="1h", start=date(2026, 1, 1), end=date(2026, 1, 2))

    assert df["timestamp"].dt.tz is not None


def test_fetch_bars_propagates_massive_errors():
    """MassiveNoDataError / MassiveAuthError should not be swallowed."""
    from xenon.clients.massive_client import MassiveNoDataError
    from scripts.ta_lib.bars import fetch_bars

    client = MagicMock()
    client.get_aggregates.side_effect = MassiveNoDataError("UNKNOWN")

    with pytest.raises(MassiveNoDataError):
        fetch_bars(client, "UNKNOWN", timeframe="1d", start=date(2026, 1, 1), end=date(2026, 1, 2))
