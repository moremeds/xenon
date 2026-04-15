"""Regression: ta-cli offline subcommands must not need MASSIVE_API_KEY."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

import scripts.ta_cli as cli


@pytest.mark.parametrize(
    "argv",
    [
        ["--stats"],
        ["--query", "SELECT 1"],
        ["--cache-only", "AAPL"],
    ],
)
@patch.dict(os.environ, {}, clear=True)
def test_offline_subcommands_dont_need_api_key(argv, monkeypatch, tmp_path):
    """These must not raise MassiveAuthError."""
    fake_svc = MagicMock()
    fake_svc.get_snapshot.return_value = {
        "ticker": "AAPL",
        "close": 1.0,
        "price": 1.0,
    }
    fake_svc.get_indicators.return_value = {"ma_20": 1.0}

    # Create a minimal DuckDB so --stats/--query open in read-only mode succeed.
    import duckdb

    db_path = tmp_path / "ta.duckdb"
    conn = duckdb.connect(str(db_path))
    conn.execute(
        "CREATE TABLE ohlc_bars ("
        "ticker VARCHAR, timeframe VARCHAR, bar_date DATE, "
        "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
        "volume BIGINT, fetched_at TIMESTAMP"
        ")"
    )
    conn.execute("CREATE TABLE ta_indicators (ticker VARCHAR, bar_date DATE)")
    conn.close()

    # Inject the temp DB into argv for offline subcommands that hit it.
    argv = list(argv) + ["--db", str(db_path)]

    monkeypatch.setattr(cli, "TAService", lambda **kw: fake_svc)
    monkeypatch.setattr(cli, "_build_massive_client", lambda: None)
    try:
        result = cli.main(argv)
    except SystemExit as e:
        assert e.code in (None, 0)
    else:
        assert result in (None, 0)
