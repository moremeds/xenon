"""Unit tests for xenon.fetchers.fetch_apex_data."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def test_cli_rejects_unknown_mode():
    from xenon.fetchers.fetch_apex_data import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--mode", "wibble"])


def test_cli_accepts_incremental_and_full():
    from xenon.fetchers.fetch_apex_data import build_parser

    parser = build_parser()
    args = parser.parse_args(["--mode", "incremental"])
    assert args.mode == "incremental"
    args = parser.parse_args(["--mode", "full", "--timeframes", "1d"])
    assert args.mode == "full"
    assert args.timeframes == ["1d"]


def test_load_universe_returns_tickers_and_timeframes():
    from xenon.fetchers.fetch_apex_data import load_universe

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
    from xenon.fetchers.fetch_apex_data import load_universe

    fake_r2 = MagicMock()
    fake_r2.get_json.return_value = {"tickers": []}
    with pytest.raises(RuntimeError, match="no tickers"):
        load_universe(fake_r2)


def test_expand_targets_only_includes_requested_timeframes():
    from xenon.fetchers.fetch_apex_data import expand_targets

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
    from xenon.fetchers.fetch_apex_data import expand_targets

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

    from xenon.fetchers.fetch_apex_data import refresh_one

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

    from xenon.fetchers.fetch_apex_data import refresh_one
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

    from xenon.fetchers.fetch_apex_data import refresh_one
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

    from xenon.fetchers.fetch_apex_data import refresh_one
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

    from xenon.clients.massive_client import MassiveNoDataError
    from xenon.fetchers.fetch_apex_data import refresh_one

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

    from xenon.fetchers.fetch_apex_data import refresh_one

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

    monkeypatch.setattr("xenon.fetchers.fetch_apex_data._compute_indicators_adapter", boom)

    result = refresh_one(r2=r2, massive=massive, ticker="AAPL", timeframe="1d", mode="full")
    assert not result.succeeded
    assert "simulated compute failure" in result.error
    r2.put_object.assert_not_called()


def test_compute_indicators_adapter_keeps_nan_during_warmup():
    """A10: 20-day rolling windows should be NaN for rows 0-18."""
    import numpy as np
    import pandas as pd

    from xenon.fetchers.fetch_apex_data import _compute_indicators_adapter

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

    from xenon.fetchers.fetch_apex_data import _compute_indicators_adapter

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


# ---------------------------------------------------------------------------
# Task 7: run_refresh parallel driver + A4/A16/A21/A22 amendments
# ---------------------------------------------------------------------------


def test_run_refresh_happy_path_writes_manifest_and_returns_zero():
    from unittest.mock import MagicMock, patch

    from xenon.fetchers.fetch_apex_data import RefreshResult, run_refresh

    r2 = MagicMock()
    r2.get_json.side_effect = [
        {
            "tickers": [
                {"symbol": "AAPL", "timeframes": ["1d"]},
                {"symbol": "MSFT", "timeframes": ["1d"]},
            ]
        },
        {},  # existing manifest (empty)
    ]

    def head_or_get(key):
        if key == "meta/last_updated.json":
            return {"ETag": '"abc"'}
        return None

    r2.head.side_effect = head_or_get

    with patch("xenon.fetchers.fetch_apex_data.refresh_one") as mock_refresh:
        mock_refresh.side_effect = lambda *, r2, massive, ticker, timeframe, mode: RefreshResult(
            ticker, timeframe, succeeded=True, rows_written=10
        )
        with patch("xenon.fetchers.fetch_apex_data.MassiveClient") as mclient:
            mclient.return_value = MagicMock()
            exit_code = run_refresh(r2=r2, mode="full", timeframes=("1d",), max_workers=2)

    assert exit_code == 0
    # Verify manifest PUT occurred
    manifest_puts = [c for c in r2.put_json.call_args_list if c.args[0] == "meta/last_updated.json"]
    assert len(manifest_puts) == 1
    payload = manifest_puts[0].args[1]
    assert "historical" in payload and "indicators" in payload
    assert payload["schema_version"] == 1


def test_run_refresh_50pct_boundary_writes_manifest_strict_gt():
    """A21: exactly 50% failure (1 of 2) is still ALLOWED (strict >)."""
    from unittest.mock import MagicMock, patch

    from xenon.fetchers.fetch_apex_data import RefreshResult, run_refresh

    r2 = MagicMock()
    r2.get_json.side_effect = [
        {"tickers": [{"symbol": "A", "timeframes": ["1d"]}, {"symbol": "B", "timeframes": ["1d"]}]},
        {},
    ]
    r2.head.return_value = None

    def rr(*, r2, massive, ticker, timeframe, mode):
        return RefreshResult(ticker, timeframe, succeeded=(ticker == "A"), error=None if ticker == "A" else "boom")

    with patch("xenon.fetchers.fetch_apex_data.refresh_one", side_effect=rr):
        with patch("xenon.fetchers.fetch_apex_data.MassiveClient") as mclient:
            mclient.return_value = MagicMock()
            exit_code = run_refresh(r2=r2, mode="full", timeframes=("1d",), max_workers=2)
    assert exit_code == 0
    manifest_puts = [c for c in r2.put_json.call_args_list if c.args[0] == "meta/last_updated.json"]
    assert len(manifest_puts) == 1, "50% is still <=0.50 under strict >, manifest should be written"


def test_run_refresh_over_50pct_failure_skips_manifest_and_returns_3():
    """A4: 60% failure (6 of 10) -> manifest NOT written, data_quality IS written."""
    from unittest.mock import MagicMock, patch

    from xenon.fetchers.fetch_apex_data import RefreshResult, run_refresh

    r2 = MagicMock()
    r2.get_json.side_effect = [
        {"tickers": [{"symbol": f"T{i}", "timeframes": ["1d"]} for i in range(10)]},
    ]
    r2.head.return_value = None

    def rr(*, r2, massive, ticker, timeframe, mode):
        idx = int(ticker[1:])
        succeeded = idx < 4  # 4 pass, 6 fail -> 60%
        return RefreshResult(ticker, timeframe, succeeded=succeeded, error=None if succeeded else "boom")

    with patch("xenon.fetchers.fetch_apex_data.refresh_one", side_effect=rr):
        with patch("xenon.fetchers.fetch_apex_data.MassiveClient") as mclient:
            mclient.return_value = MagicMock()
            exit_code = run_refresh(r2=r2, mode="full", timeframes=("1d",), max_workers=2)

    assert exit_code == 3, f"expected 3 on degraded run, got {exit_code}"
    # Manifest NOT written
    manifest_puts = [c for c in r2.put_json.call_args_list if c.args[0] == "meta/last_updated.json"]
    assert len(manifest_puts) == 0, "manifest MUST NOT be written on degraded run (A4)"
    # data_quality IS written
    dq_puts = [c for c in r2.put_json.call_args_list if c.args[0] == "meta/data_quality.json"]
    assert len(dq_puts) == 1


def test_run_refresh_zero_percent_failure_writes_manifest():
    """A21: 0% failure is the happy path; double-check it writes."""
    from unittest.mock import MagicMock, patch

    from xenon.fetchers.fetch_apex_data import RefreshResult, run_refresh

    r2 = MagicMock()
    r2.get_json.side_effect = [
        {"tickers": [{"symbol": "A", "timeframes": ["1d"]}]},
        {},
    ]
    r2.head.return_value = None

    with patch("xenon.fetchers.fetch_apex_data.refresh_one") as mock_refresh:
        mock_refresh.side_effect = lambda *, r2, massive, ticker, timeframe, mode: RefreshResult(
            ticker, timeframe, succeeded=True, rows_written=1
        )
        with patch("xenon.fetchers.fetch_apex_data.MassiveClient") as mclient:
            mclient.return_value = MagicMock()
            exit_code = run_refresh(r2=r2, mode="full", timeframes=("1d",), max_workers=1)
    assert exit_code == 0
    manifest_puts = [c for c in r2.put_json.call_args_list if c.args[0] == "meta/last_updated.json"]
    assert len(manifest_puts) == 1


def test_update_manifest_with_retry_retries_on_precondition_failure():
    """A16: R2PreconditionError -> retry up to 3 attempts before raising."""
    from unittest.mock import MagicMock, patch

    from xenon.fetchers.fetch_apex_data import _update_manifest_with_retry
    from scripts.ta_lib.r2_store import R2PreconditionError

    r2 = MagicMock()
    r2.head.return_value = {"ETag": '"etag1"'}
    r2.get_json.return_value = {"schema_version": 1}

    # First two attempts raise; third succeeds
    r2.put_json.side_effect = [R2PreconditionError("mismatch"), R2PreconditionError("mismatch"), '"new-etag"']

    with patch("xenon.fetchers.fetch_apex_data.time.sleep") as sleep_mock:
        _update_manifest_with_retry(r2)

    assert r2.put_json.call_count == 3
    assert sleep_mock.call_count == 2  # between attempts 0->1 and 1->2


def test_update_manifest_with_retry_raises_after_max_attempts():
    from unittest.mock import MagicMock, patch

    from xenon.fetchers.fetch_apex_data import _update_manifest_with_retry
    from scripts.ta_lib.r2_store import R2PreconditionError

    r2 = MagicMock()
    r2.head.return_value = None
    r2.put_json.side_effect = R2PreconditionError("persistent")

    with patch("xenon.fetchers.fetch_apex_data.time.sleep"):
        with pytest.raises(R2PreconditionError):
            _update_manifest_with_retry(r2, attempts=3)
    assert r2.put_json.call_count == 3


def test_default_max_workers_is_5_per_a22():
    """A22: conservative default to reduce MassiveClient session contention."""
    from xenon.fetchers.fetch_apex_data import _DEFAULT_MAX_WORKERS, build_parser

    assert _DEFAULT_MAX_WORKERS == 5
    args = build_parser().parse_args(["--mode", "full"])
    assert args.max_workers == 5


# ---------------------------------------------------------------------------
# Task 13: A18 session-completeness guard
# ---------------------------------------------------------------------------


def test_a18_prior_trading_day_weekday_after_close():
    """A monday afternoon 17:00 ET → prior trading day is the same monday."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from xenon.fetchers.fetch_apex_data import _prior_trading_day

    # 2025-11-17 = Monday, 17:00 ET (post-close)
    now = datetime(2025, 11, 17, 17, 0, tzinfo=ZoneInfo("America/New_York"))
    assert _prior_trading_day(now).isoformat() == "2025-11-17"


def test_a18_prior_trading_day_weekday_before_close():
    """Monday 12:00 ET → prior trading day is prior Friday."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from xenon.fetchers.fetch_apex_data import _prior_trading_day

    # 2025-11-17 = Monday, 12:00 ET (pre-close) → expect Friday 2025-11-14
    now = datetime(2025, 11, 17, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    assert _prior_trading_day(now).isoformat() == "2025-11-14"


def test_a18_prior_trading_day_saturday():
    """Saturday (any time) → prior trading day = prior Friday."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from xenon.fetchers.fetch_apex_data import _prior_trading_day

    now = datetime(2025, 11, 15, 10, 0, tzinfo=ZoneInfo("America/New_York"))  # Saturday
    assert _prior_trading_day(now).isoformat() == "2025-11-14"


def test_a18_prior_trading_day_sunday():
    """Sunday → prior Friday."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from xenon.fetchers.fetch_apex_data import _prior_trading_day

    now = datetime(2025, 11, 16, 23, 0, tzinfo=ZoneInfo("America/New_York"))
    assert _prior_trading_day(now).isoformat() == "2025-11-14"


def test_a18_session_not_ready_pre_close_weekday():
    from datetime import datetime
    from unittest.mock import MagicMock
    from zoneinfo import ZoneInfo

    from xenon.fetchers.fetch_apex_data import _incremental_session_ready

    r2 = MagicMock()
    now = datetime(2025, 11, 17, 12, 0, tzinfo=ZoneInfo("America/New_York"))  # Mon 12:00
    ready, reason = _incremental_session_ready(r2, now_et=now)
    assert not ready
    assert "pre-close" in reason


def test_a18_session_ready_post_close_with_spy_available(monkeypatch):
    """Happy path: post-close weekday, SPY 1d probe succeeds → ready=True."""
    from datetime import datetime
    from unittest.mock import MagicMock
    from zoneinfo import ZoneInfo

    from xenon.fetchers.fetch_apex_data import _incremental_session_ready

    r2 = MagicMock()
    now = datetime(2025, 11, 17, 17, 0, tzinfo=ZoneInfo("America/New_York"))

    # Patch MassiveClient + fetch_bars to succeed
    massive = MagicMock()
    monkeypatch.setattr("xenon.fetchers.fetch_apex_data.MassiveClient", lambda: massive)
    monkeypatch.setattr("xenon.fetchers.fetch_apex_data.fetch_bars", lambda *a, **kw: object())

    ready, reason = _incremental_session_ready(r2, now_et=now)
    assert ready, reason


def test_a18_session_not_ready_when_spy_bar_missing(monkeypatch):
    """Post-close but Massive hasn't published yesterday's SPY → defer."""
    from datetime import datetime
    from unittest.mock import MagicMock
    from zoneinfo import ZoneInfo

    from xenon.clients.massive_client import MassiveNoDataError
    from xenon.fetchers.fetch_apex_data import _incremental_session_ready

    r2 = MagicMock()
    now = datetime(2025, 11, 17, 17, 0, tzinfo=ZoneInfo("America/New_York"))

    massive = MagicMock()

    def raise_no_data(*a, **kw):
        raise MassiveNoDataError("SPY 1d not ready")

    monkeypatch.setattr("xenon.fetchers.fetch_apex_data.MassiveClient", lambda: massive)
    monkeypatch.setattr("xenon.fetchers.fetch_apex_data.fetch_bars", raise_no_data)

    ready, reason = _incremental_session_ready(r2, now_et=now)
    assert not ready
    assert "not published" in reason


def test_main_skips_run_refresh_when_a18_not_ready(monkeypatch, capsys):
    """main() should exit 0 without running the refresh when session isn't ready."""
    from unittest.mock import MagicMock

    import xenon.fetchers.fetch_apex_data as apex

    # Make the session-ready probe return False
    monkeypatch.setattr(apex, "_incremental_session_ready", lambda r2, now_et=None: (False, "stubbed"))
    run_calls = []
    monkeypatch.setattr(apex, "run_refresh", lambda **kw: run_calls.append(kw) or 0)
    monkeypatch.setattr(apex, "DryRunStore", lambda path: MagicMock(), raising=False)

    # Use dry-run to avoid needing real R2 env
    rc = apex.main(["--mode", "incremental", "--dry-run"])
    assert rc == 0
    assert run_calls == [], "run_refresh should NOT be called when A18 defers"


def test_main_full_mode_bypasses_a18(monkeypatch):
    """Full mode skips the A18 guard entirely."""
    from unittest.mock import MagicMock

    import xenon.fetchers.fetch_apex_data as apex

    called = []
    monkeypatch.setattr(
        apex, "_incremental_session_ready", lambda r2, now_et=None: called.append(True) or (False, "stub")
    )
    monkeypatch.setattr(apex, "run_refresh", lambda **kw: 0)
    monkeypatch.setattr(apex, "DryRunStore", lambda path: MagicMock(), raising=False)

    rc = apex.main(["--mode", "full", "--dry-run"])
    assert rc == 0
    assert called == [], "A18 guard must NOT run in full mode"


# ---------------------------------------------------------------------------
# Task 1 (T1): Narrow A18 probe exception handling
# ---------------------------------------------------------------------------


def test_a18_session_not_ready_on_massive_auth_error(monkeypatch):
    """T1: MassiveAuthError must NOT fall through to 'proceed anyway'."""
    from datetime import datetime
    from unittest.mock import MagicMock
    from zoneinfo import ZoneInfo

    from xenon.clients.massive_client import MassiveAuthError
    from xenon.fetchers.fetch_apex_data import _incremental_session_ready

    r2 = MagicMock()
    now = datetime(2025, 11, 17, 17, 0, tzinfo=ZoneInfo("America/New_York"))

    def raise_auth():
        raise MassiveAuthError("MASSIVE_API_KEY not set")

    monkeypatch.setattr("xenon.fetchers.fetch_apex_data.MassiveClient", raise_auth)

    ready, reason = _incremental_session_ready(r2, now_et=now)
    assert not ready
    assert "MassiveAuthError" in reason or "MASSIVE_API_KEY" in reason


def test_a18_session_tolerates_transient_network_probe_failure(monkeypatch):
    """T1: genuinely transient probe errors (requests.RequestException) still 'proceed'
    so a flaky DNS blip doesn't kill the nightly."""
    from datetime import datetime
    from unittest.mock import MagicMock
    from zoneinfo import ZoneInfo

    import requests

    from xenon.fetchers.fetch_apex_data import _incremental_session_ready

    r2 = MagicMock()
    now = datetime(2025, 11, 17, 17, 0, tzinfo=ZoneInfo("America/New_York"))

    massive = MagicMock()
    monkeypatch.setattr("xenon.fetchers.fetch_apex_data.MassiveClient", lambda: massive)

    def flake(*a, **kw):
        raise requests.ConnectionError("DNS blip")

    monkeypatch.setattr("xenon.fetchers.fetch_apex_data.fetch_bars", flake)

    ready, reason = _incremental_session_ready(r2, now_et=now)
    assert ready, f"transient network probe error should still proceed; got reason={reason}"


def test_a18_session_defers_on_unknown_probe_error(monkeypatch):
    """T1: unexpected exceptions from the probe should fail CLOSED (defer), not proceed."""
    from datetime import datetime
    from unittest.mock import MagicMock
    from zoneinfo import ZoneInfo

    from xenon.fetchers.fetch_apex_data import _incremental_session_ready

    r2 = MagicMock()
    now = datetime(2025, 11, 17, 17, 0, tzinfo=ZoneInfo("America/New_York"))

    massive = MagicMock()
    monkeypatch.setattr("xenon.fetchers.fetch_apex_data.MassiveClient", lambda: massive)

    def weird(*a, **kw):
        raise ValueError("unexpected vendor response shape")

    monkeypatch.setattr("xenon.fetchers.fetch_apex_data.fetch_bars", weird)

    ready, reason = _incremental_session_ready(r2, now_et=now)
    assert not ready
    assert "ValueError" in reason or "unexpected" in reason


# ---------------------------------------------------------------------------
# Task 3 (T5): Sanitize inf from indicator division-by-zero
# ---------------------------------------------------------------------------


def test_refresh_one_rolls_back_historical_put_when_indicator_put_fails():
    """T2: if indicator PUT fails after historical PUT succeeded, delete the
    historical object so the ticker's two-parquet state stays consistent
    (both present or neither)."""
    from unittest.mock import MagicMock

    import pandas as pd

    from xenon.fetchers.fetch_apex_data import refresh_one

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

    r2 = MagicMock()
    written: dict[str, bytes] = {}

    hist_key = "parquet/historical/1d/AAPL.parquet"
    ind_key = "parquet/indicators/1d/AAPL.parquet"

    def fake_put(key, body, if_match=None):
        if key == ind_key:
            raise RuntimeError("simulated indicator PUT failure")
        written[key] = body
        return '"etag"'

    deleted: list[str] = []

    def fake_delete(key):
        deleted.append(key)
        written.pop(key, None)

    r2.put_object.side_effect = fake_put
    r2.delete_object.side_effect = fake_delete

    result = refresh_one(r2=r2, massive=massive, ticker="AAPL", timeframe="1d", mode="full")

    assert not result.succeeded
    # Historical was PUT then rolled back
    assert hist_key in deleted, f"expected historical rollback; deletes={deleted!r}"
    assert hist_key not in written, "historical object must be gone after rollback"


def test_compute_indicators_adapter_handles_zero_close_without_inf():
    """T5: a zero close row must not emit inf for atr_pct / range_20d_pct."""
    import numpy as np
    import pandas as pd

    from xenon.fetchers.fetch_apex_data import _compute_indicators_adapter

    n = 60
    # Day 30 has close=0 (pathological but defensible to guard)
    closes = [100.0 + i * 0.1 for i in range(n)]
    closes[29] = 0.0
    ohlcv = pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC"),
            "open": closes,
            "high": [c + 0.5 for c in closes],
            "low": [c - 0.5 for c in closes],
            "close": closes,
            "volume": [1_000_000] * n,
        }
    )
    ind = _compute_indicators_adapter(ohlcv)

    # the zero-close row itself may be NaN but MUST NOT be inf
    assert not np.isinf(ind["atr_pct"]).any(), "atr_pct must never be +/-inf"
    assert not np.isinf(ind["range_20d_pct"]).any(), "range_20d_pct must never be +/-inf"
