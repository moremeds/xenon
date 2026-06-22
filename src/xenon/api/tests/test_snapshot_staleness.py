"""Unit tests for `_snapshot_age_seconds` — the loud-not-silent guard that
catches a /portfolio/sync which exits 0 but never persisted a fresh snapshot
(the schema-drift class of silent failure from 2026-06-22).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from xenon.api.server import _snapshot_age_seconds


def test_fresh_snapshot_age_is_small():
    payload = {"last_sync": datetime.now().isoformat()}
    age = _snapshot_age_seconds(payload)
    assert age is not None
    assert 0 <= age < 30


def test_stale_snapshot_age_reflects_last_sync():
    stamped = (datetime.now() - timedelta(seconds=600)).isoformat()
    age = _snapshot_age_seconds({"last_sync": stamped})
    assert age is not None
    assert 590 <= age < 700  # ~600s, with slack for test runtime


def test_missing_last_sync_returns_none():
    assert _snapshot_age_seconds({}) is None
    assert _snapshot_age_seconds({"positions": []}) is None


def test_unparseable_last_sync_returns_none():
    assert _snapshot_age_seconds({"last_sync": "not-a-timestamp"}) is None
    assert _snapshot_age_seconds({"last_sync": None}) is None


def test_non_dict_payload_returns_none():
    assert _snapshot_age_seconds(None) is None  # type: ignore[arg-type]
