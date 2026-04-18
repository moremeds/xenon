"""Tests for scanner_lib JSON cache writer."""

from __future__ import annotations

import json

import pytest


def test_write_cache_creates_file(tmp_path):
    from scanners._shared.cache import write_json_cache

    path = tmp_path / "scan.json"
    data = {"scan_id": "test_001", "candidates": []}
    write_json_cache(path, data)
    assert path.exists()
    loaded = json.loads(path.read_text())
    assert loaded["scan_id"] == "test_001"


def test_write_cache_overwrites_existing(tmp_path):
    from scanners._shared.cache import write_json_cache

    path = tmp_path / "scan.json"
    write_json_cache(path, {"version": 1})
    write_json_cache(path, {"version": 2})
    loaded = json.loads(path.read_text())
    assert loaded["version"] == 2


def test_write_cache_creates_parent_dirs(tmp_path):
    from scanners._shared.cache import write_json_cache

    path = tmp_path / "nested" / "dir" / "scan.json"
    write_json_cache(path, {"ok": True})
    assert path.exists()


def test_read_cache_returns_data(tmp_path):
    from scanners._shared.cache import read_json_cache, write_json_cache

    path = tmp_path / "scan.json"
    write_json_cache(path, {"ticker": "AAPL"})
    data = read_json_cache(path)
    assert data is not None
    assert data["ticker"] == "AAPL"


def test_read_cache_returns_none_for_missing(tmp_path):
    from scanners._shared.cache import read_json_cache

    data = read_json_cache(tmp_path / "nonexistent.json")
    assert data is None


def test_read_cache_staleness(tmp_path):
    import time

    from scanners._shared.cache import read_json_cache, write_json_cache

    path = tmp_path / "scan.json"
    write_json_cache(path, {"val": 1})
    data = read_json_cache(path, max_age_secs=9999)
    assert data is not None
    assert data["val"] == 1
    from scanners._shared.cache import is_stale

    assert not is_stale(path, max_age_secs=9999)
