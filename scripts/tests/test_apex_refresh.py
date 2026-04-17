"""Unit tests for scripts.apex_refresh."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_cli_rejects_unknown_mode():
    from scripts.apex_refresh import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--mode", "wibble"])


def test_cli_accepts_incremental_and_full():
    from scripts.apex_refresh import build_parser

    parser = build_parser()
    args = parser.parse_args(["--mode", "incremental"])
    assert args.mode == "incremental"
    args = parser.parse_args(["--mode", "full", "--timeframes", "1d"])
    assert args.mode == "full"
    assert args.timeframes == ["1d"]


def test_load_universe_returns_tickers_and_timeframes():
    from scripts.apex_refresh import load_universe

    fake_r2 = MagicMock()
    fake_r2.get_json.return_value = {
        "tickers": [
            {"symbol": "AAPL", "timeframes": ["1d", "1h"]},
            {"symbol": "MSFT", "timeframes": ["1d"]},
        ]
    }
    rows = load_universe(fake_r2)
    assert len(rows) == 2
    assert rows[0]["symbol"] == "AAPL"
    fake_r2.get_json.assert_called_once_with("meta/universe.json")


def test_load_universe_raises_on_empty_tickers():
    from scripts.apex_refresh import load_universe

    fake_r2 = MagicMock()
    fake_r2.get_json.return_value = {"tickers": []}
    with pytest.raises(RuntimeError, match="no tickers"):
        load_universe(fake_r2)


def test_expand_targets_only_includes_requested_timeframes():
    from scripts.apex_refresh import expand_targets

    universe = [
        {"symbol": "AAPL", "timeframes": ["1d", "1h", "4h"]},
        {"symbol": "BOND", "timeframes": ["1d"]},  # no 1h
    ]
    targets = list(expand_targets(universe, timeframes=("1d", "1h")))
    assert ("AAPL", "1d") in targets
    assert ("AAPL", "1h") in targets
    assert ("BOND", "1d") in targets
    assert ("BOND", "1h") not in targets


def test_expand_targets_skips_rows_without_symbol():
    from scripts.apex_refresh import expand_targets

    universe = [
        {"symbol": "AAPL", "timeframes": ["1d"]},
        {"timeframes": ["1d"]},  # missing symbol
    ]
    targets = list(expand_targets(universe, timeframes=("1d",)))
    assert targets == [("AAPL", "1d")]


# ---------------------------------------------------------------------------
# Task 6: refresh_one, _compute_indicators_adapter, DryRunStore
# ---------------------------------------------------------------------------


def test_refresh_one_full_mode_writes_both_parquets():
    """Full mode: fetch 2y from Massive, compute indicators, write both parquets."""
    from unittest.mock import MagicMock

    import pandas as pd

    from scripts.apex_refresh import refresh_one

    # MassiveClient stub — actually called via bars.fetch_bars which wraps get_aggregates
    massive = MagicMock()
    n = 300  # enough for 252-day rolling
    massive.get_aggregates.return_value = pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=n, freq="D", tz="America/New_York"),
            "open": [100.0 + i * 0.1 for i in range(n)],
            "high": [101.0 + i * 0.1 for i in range(n)],
            "low": [99.0 + i * 0.1 for i in range(n)],
            "close": [100.5 + i * 0.1 for i in range(n)],
            "volume": [1_000_000] * n,
            "vwap": [100.25 + i * 0.1 for i in range(n)],
            "tx_count": [150] * n,
        }
    )

    r2 = MagicMock()
    written: dict[str, bytes] = {}

    def fake_put(key, body, if_match=None):
        written[key] = body
        return '"etag"'

    r2.put_object.side_effect = fake_put

    result = refresh_one(r2=r2, massive=massive, ticker="AAPL", timeframe="1d", mode="full")

    assert result.succeeded, f"expected success, got error: {result.error}"
    assert "parquet/historical/1d/AAPL.parquet" in written
    assert "parquet/indicators/1d/AAPL.parquet" in written
    r2.get_object.assert_not_called()  # full mode skips existing


def test_refresh_one_incremental_reads_existing_and_dedupes():
    """Incremental mode: read existing, fetch gap from Massive, dedupe, write both."""
    import io
    from unittest.mock import MagicMock

    import pandas as pd

    from scripts.apex_refresh import refresh_one
    from scripts.ta_lib.parquet_store import write_ohlcv

    # Build an existing parquet (300 bars ending 2025-10-26)
    n = 300
    existing_df = pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=n, freq="D", tz="America/New_York"),
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [1_000_000] * n,
        }
    )
    hist_buf = io.BytesIO()
    write_ohlcv(hist_buf, existing_df, timeframe="1d")
    existing_bytes = hist_buf.getvalue()

    r2 = MagicMock()
    r2.get_object.return_value = existing_bytes
    written: dict[str, bytes] = {}
    r2.put_object.side_effect = lambda k, b, if_match=None: written.__setitem__(k, b) or '"etag"'

    # Massive returns 5 new bars starting from the day after last_ts
    massive = MagicMock()
    new_n = 5
    massive.get_aggregates.return_value = pd.DataFrame(
        {
            "date": pd.date_range("2025-10-27", periods=new_n, freq="D", tz="America/New_York"),
            "open": [200.0] * new_n,
            "high": [201.0] * new_n,
            "low": [199.0] * new_n,
            "close": [200.5] * new_n,
            "volume": [2_000_000] * new_n,
            "vwap": [200.25] * new_n,
            "tx_count": [175] * new_n,
        }
    )

    result = refresh_one(r2=r2, massive=massive, ticker="AAPL", timeframe="1d", mode="incremental")

    assert result.succeeded, f"error: {result.error}"
    r2.get_object.assert_called_once_with("parquet/historical/1d/AAPL.parquet")
    assert "parquet/historical/1d/AAPL.parquet" in written
    assert "parquet/indicators/1d/AAPL.parquet" in written


def test_refresh_one_incremental_cold_start_when_no_existing():
    """A12: R2NotFoundError on existing parquet triggers cold-start lookback."""
    from unittest.mock import MagicMock

    import pandas as pd

    from scripts.apex_refresh import refresh_one
    from scripts.ta_lib.r2_store import R2NotFoundError

    r2 = MagicMock()
    r2.get_object.side_effect = R2NotFoundError("missing")
    r2.put_object.return_value = '"etag"'

    n = 300
    massive = MagicMock()
    massive.get_aggregates.return_value = pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=n, freq="D", tz="America/New_York"),
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [1_000_000] * n,
            "vwap": [100.25] * n,
            "tx_count": [150] * n,
        }
    )

    result = refresh_one(r2=r2, massive=massive, ticker="NEWCO", timeframe="1d", mode="incremental")
    assert result.succeeded, result.error
    # In cold-start, Massive is called with a ~2y window
    args = massive.get_aggregates.call_args
    from_date = args.args[2] if len(args.args) > 2 else args.kwargs.get("from_date")
    # from_date is an ISO string of date.today() - 730 days
    from datetime import date, timedelta

    expected_from = (date.today() - timedelta(days=730)).isoformat()
    assert from_date == expected_from


def test_refresh_one_hourly_incremental_uses_same_date_start():
    """A11: hourly incremental picks start=last_ts.date() (not last_ts + 1 day),
    relying on dedupe_concat to drop overlap. This is the hourly-boundary fix."""
    import io
    from unittest.mock import MagicMock

    import pandas as pd

    from scripts.apex_refresh import refresh_one
    from scripts.ta_lib.parquet_store import write_ohlcv

    # Existing file: hourly bars up to 2025-10-26 15:00 ET (last trading hour)
    existing = pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-10-26 09:30", periods=7, freq="h", tz="America/New_York"),
            "open": [100.0] * 7,
            "high": [101.0] * 7,
            "low": [99.0] * 7,
            "close": [100.5] * 7,
            "volume": [500_000] * 7,
        }
    )
    hist_buf = io.BytesIO()
    write_ohlcv(hist_buf, existing, timeframe="1h")

    r2 = MagicMock()
    r2.get_object.return_value = hist_buf.getvalue()
    r2.put_object.return_value = '"etag"'

    massive = MagicMock()
    # Massive returns overlap (6 more hourly bars on the same date)
    massive.get_aggregates.return_value = pd.DataFrame(
        {
            "date": pd.date_range("2025-10-26 10:30", periods=6, freq="h", tz="America/New_York"),
            "open": [200.0] * 6,
            "high": [201.0] * 6,
            "low": [199.0] * 6,
            "close": [200.5] * 6,
            "volume": [1_000_000] * 6,
            "vwap": [200.25] * 6,
            "tx_count": [175] * 6,
        }
    )

    result = refresh_one(r2=r2, massive=massive, ticker="AAPL", timeframe="1h", mode="incremental")
    assert result.succeeded, result.error
    # A11: from_date should be 2025-10-26, NOT 2025-10-27
    args = massive.get_aggregates.call_args
    from_date = args.args[2] if len(args.args) > 2 else args.kwargs.get("from_date")
    assert from_date == "2025-10-26"


def test_refresh_one_captures_massive_failure_without_raising():
    from unittest.mock import MagicMock

    from scripts.apex_refresh import refresh_one
    from scripts.clients.massive_client import MassiveNoDataError

    massive = MagicMock()
    massive.get_aggregates.side_effect = MassiveNoDataError("UNKNOWN_TICKER")

    r2 = MagicMock()
    result = refresh_one(r2=r2, massive=massive, ticker="XYZZY", timeframe="1d", mode="full")
    assert not result.succeeded
    assert "MassiveNoDataError" in result.error
    r2.put_object.assert_not_called()


def test_refresh_one_a5_atomicity_no_put_when_indicator_compute_fails(monkeypatch):
    """A5: if indicator compute raises, neither parquet is PUT."""
    from unittest.mock import MagicMock

    import pandas as pd

    from scripts.apex_refresh import refresh_one

    r2 = MagicMock()
    massive = MagicMock()
    n = 300
    massive.get_aggregates.return_value = pd.DataFrame(
        {
            "date": pd.date_range("2025-01-01", periods=n, freq="D", tz="America/New_York"),
            "open": [100.0] * n,
            "high": [101.0] * n,
            "low": [99.0] * n,
            "close": [100.5] * n,
            "volume": [1_000_000] * n,
            "vwap": [100.25] * n,
            "tx_count": [150] * n,
        }
    )

    # Force the indicator adapter to fail
    def boom(_):
        raise RuntimeError("simulated compute failure")

    monkeypatch.setattr("scripts.apex_refresh._compute_indicators_adapter", boom)

    result = refresh_one(r2=r2, massive=massive, ticker="AAPL", timeframe="1d", mode="full")
    assert not result.succeeded
    assert "simulated compute failure" in result.error
    r2.put_object.assert_not_called()


def test_compute_indicators_adapter_keeps_nan_during_warmup():
    """A10: 20-day rolling windows should be NaN for rows 0-18."""
    import numpy as np
    import pandas as pd

    from scripts.apex_refresh import _compute_indicators_adapter

    n = 30
    ohlcv = pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC"),
            "open": [100.0 + i for i in range(n)],
            "high": [101.0 + i for i in range(n)],
            "low": [99.0 + i for i in range(n)],
            "close": [100.5 + i for i in range(n)],
            "volume": [1_000_000] * n,
        }
    )
    ind = _compute_indicators_adapter(ohlcv)
    # Rows 0-18 should have NaN high_20d
    assert ind["high_20d"].iloc[0:19].isna().all()
    # Row 19 onward should be filled
    assert not np.isnan(ind["high_20d"].iloc[19])
    # 52w stays NaN since n < 252
    assert ind["high_52w"].isna().all()


def test_compute_indicators_adapter_includes_a3_fields():
    """A3: recent_avg_volume, avg_20d_volume, recent_up_ratio, range_20d_pct, atr_pct present."""
    import pandas as pd

    from scripts.apex_refresh import _compute_indicators_adapter

    n = 300
    ohlcv = pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC"),
            "open": [100.0 + i * 0.1 for i in range(n)],
            "high": [101.0 + i * 0.1 for i in range(n)],
            "low": [99.0 + i * 0.1 for i in range(n)],
            "close": [100.5 + i * 0.1 for i in range(n)],
            "volume": [1_000_000] * n,
        }
    )
    ind = _compute_indicators_adapter(ohlcv)
    for col in ("recent_avg_volume", "avg_20d_volume", "recent_up_ratio", "range_20d_pct", "atr_pct"):
        assert col in ind.columns, col
        assert not ind[col].tail(50).isna().all(), f"{col} all NaN in tail"


def test_dry_run_store_writes_to_local_filesystem(tmp_path):
    """A17: DryRunStore must write to a directory under tmp, not R2."""
    from scripts.ta_lib.dry_run_store import DryRunStore

    r2 = DryRunStore(tmp_path / "preview")
    r2.put_object("meta/test.json", b'{"ok": true}')
    assert (tmp_path / "preview" / "meta" / "test.json").exists()
    got = r2.get_object("meta/test.json")
    assert got == b'{"ok": true}'


def test_dry_run_store_get_object_raises_r2_not_found_on_missing(tmp_path):
    """A17: DryRunStore uses the same exception type as R2Store so callers are interchangeable."""
    from scripts.ta_lib.dry_run_store import DryRunStore

    from scripts.ta_lib.r2_store import R2NotFoundError

    r2 = DryRunStore(tmp_path / "preview")
    with pytest.raises(R2NotFoundError):
        r2.get_object("nope")
