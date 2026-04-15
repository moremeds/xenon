"""Trend scan surfaces a warning when ta_premarket_status.json indicates abort."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from scripts.api_status.ta_premarket_status import TaPremarketStatus, save_status


def test_trend_scan_warns_on_aborted_status(tmp_path):
    status_path = tmp_path / "ta_premarket_status.json"
    save_status(
        status_path,
        TaPremarketStatus(
            last_run_ts=datetime(2026, 4, 15, 6, 5, tzinfo=timezone.utc),
            status="aborted",
            refreshed_tickers=10,
            failed_tickers=200,
            failure_ratio=0.95,
            failure_reason="vendor_down",
        ),
    )
    with patch("scripts.trend_scan.STATUS_FILE", status_path):
        from scripts.trend_scan import _check_premarket_status

        warn = _check_premarket_status()
    assert warn is not None
    assert "aborted" in warn.lower() or "vendor_down" in warn.lower()


def test_trend_scan_warns_on_stale_status_file_age(tmp_path):
    """Even status=ok but file older than 1 trading day should warn."""
    status_path = tmp_path / "ta_premarket_status.json"
    save_status(
        status_path,
        TaPremarketStatus(
            last_run_ts=datetime.now(timezone.utc) - timedelta(days=3),
            status="ok",
            refreshed_tickers=250,
            failed_tickers=0,
            failure_ratio=0.0,
            failure_reason=None,
        ),
    )
    with patch("scripts.trend_scan.STATUS_FILE", status_path):
        from scripts.trend_scan import _check_premarket_status

        warn = _check_premarket_status()
    assert warn is not None
    assert "stale" in warn.lower() or "old" in warn.lower()


def test_trend_scan_no_warning_when_fresh_ok(tmp_path):
    status_path = tmp_path / "ta_premarket_status.json"
    save_status(
        status_path,
        TaPremarketStatus(
            last_run_ts=datetime.now(timezone.utc),
            status="ok",
            refreshed_tickers=250,
            failed_tickers=0,
            failure_ratio=0.0,
            failure_reason=None,
        ),
    )
    with patch("scripts.trend_scan.STATUS_FILE", status_path):
        from scripts.trend_scan import _check_premarket_status

        assert _check_premarket_status() is None


def test_trend_scan_no_warning_when_status_file_missing(tmp_path):
    """No status file at all (e.g. first run) should not warn — prep may not have run yet."""
    missing_path = tmp_path / "does_not_exist.json"
    with patch("scripts.trend_scan.STATUS_FILE", missing_path):
        from scripts.trend_scan import _check_premarket_status

        assert _check_premarket_status() is None


def test_trend_scan_output_includes_warnings_field():
    """The output dict written to data/trend_scan.json must include a 'warnings' list."""
    from scripts.trend_scan import build_scan_output

    out = build_scan_output(candidates=[], warnings=["test warning"])
    assert "warnings" in out
    assert out["warnings"] == ["test warning"]


def test_build_scan_output_preserves_existing_fields():
    """build_scan_output must still produce all top-level keys trend_scan.json consumers rely on."""
    from scripts.trend_scan import build_scan_output

    out = build_scan_output(
        candidates=[],
        warnings=[],
        universe_size=42,
        stage_a_survivors=10,
        stage_b_survivors=5,
        market_context={"spy_close": 520.0, "vix_close": 17.0, "regime": "bullish"},
    )
    for key in [
        "scan_id",
        "scan_timestamp",
        "market_context",
        "universe_size",
        "stage_a_survivors",
        "stage_b_survivors",
        "candidates",
        "warnings",
    ]:
        assert key in out, f"Missing top-level key: {key}"
    assert out["universe_size"] == 42
    assert out["stage_a_survivors"] == 10
    assert out["stage_b_survivors"] == 5
    assert out["market_context"]["regime"] == "bullish"


def test_build_scan_output_defaults_warnings_to_empty_list():
    from scripts.trend_scan import build_scan_output

    out = build_scan_output(candidates=[])
    assert out["warnings"] == []
