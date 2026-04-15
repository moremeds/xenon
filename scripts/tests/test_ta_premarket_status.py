"""Tests for scripts.api_status.ta_premarket_status."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.api_status.ta_premarket_status import (
    TaPremarketStatus,
    load_status,
    save_status,
)


def test_roundtrip(tmp_path: Path):
    path = tmp_path / "status.json"
    status = TaPremarketStatus(
        last_run_ts=datetime(2026, 4, 15, 6, 5, tzinfo=timezone.utc),
        status="ok",
        refreshed_tickers=247,
        failed_tickers=3,
        failure_ratio=0.012,
        failure_reason=None,
    )
    save_status(path, status)
    loaded = load_status(path)
    assert loaded == status


def test_load_missing_returns_none(tmp_path: Path):
    assert load_status(tmp_path / "nope.json") is None


def test_load_malformed_returns_none(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    assert load_status(path) is None


def test_aborted_status(tmp_path: Path):
    path = tmp_path / "status.json"
    status = TaPremarketStatus(
        last_run_ts=datetime(2026, 4, 15, 6, 5, tzinfo=timezone.utc),
        status="aborted",
        refreshed_tickers=50,
        failed_tickers=200,
        failure_ratio=0.80,
        failure_reason="vendor_down",
    )
    save_status(path, status)
    raw = json.loads(path.read_text())
    assert raw["status"] == "aborted"
    assert raw["failure_reason"] == "vendor_down"
