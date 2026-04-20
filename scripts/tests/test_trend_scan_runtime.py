"""Runtime regressions for the trend scanner CLI."""

from __future__ import annotations

import builtins
import importlib
import json
import sys


def test_trend_scan_imports_without_duckdb(monkeypatch):
    """Verify that trend_scan module works when duckdb is not installed."""
    import xenon.scanners.trend.storage as storage_mod

    monkeypatch.setattr(storage_mod, "_duckdb", None)
    assert not storage_mod.duckdb_available()

    # The pipeline function should still be callable
    from xenon.scanners.trend import cli as trend_scan

    assert callable(trend_scan.run_scan_pipeline)


def test_main_emits_json_payload(monkeypatch, capsys, tmp_path):
    from xenon.scanners.trend import cli as trend_scan

    class DummyFetcher:
        pass

    def fake_build_runtime():
        return DummyFetcher(), None, None, None

    def fake_run_scan_pipeline(*args, **kwargs):
        return {
            "scan_id": "trend_test",
            "scan_timestamp": "2026-04-10T09:30:00+00:00",
            "market_context": {
                "spy_close": 520.0,
                "vix_close": 18.0,
                "regime": "bullish",
            },
            "universe_size": 2,
            "stage_a_survivors": 1,
            "stage_b_survivors": 1,
            "candidates": [],
        }

    monkeypatch.setattr(trend_scan, "build_runtime", fake_build_runtime)
    monkeypatch.setattr(trend_scan, "run_scan_pipeline", fake_run_scan_pipeline)

    exit_code = trend_scan.main(
        [
            "--top",
            "1",
            "--db-path",
            str(tmp_path / "trend.duckdb"),
            "--json-cache",
            str(tmp_path / "trend_scan.json"),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["scan_id"] == "trend_test"
    assert payload["market_context"]["regime"] == "bullish"


def test_pre_cache_spy_swallows_failure():
    """If SPY indicators fetch raises, pre_cache_spy must not propagate —
    scan continues with rs_vs_spy=1.0 default via existing branch in fetch_ohlcv."""
    from unittest.mock import MagicMock

    from xenon.scanners.trend.cli import LiveTrendDataFetcher

    failing_svc = MagicMock()
    failing_svc.get_indicators.side_effect = RuntimeError("SPY cold")

    fetcher = LiveTrendDataFetcher(uw_client=MagicMock(), ta_service=failing_svc)

    # Must not raise
    fetcher.pre_cache_spy()

    # Must leave _spy_df as None so fetch_ohlcv falls back to rs_vs_spy=1.0
    assert fetcher._spy_df is None
