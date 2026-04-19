"""Tests for scanner_lib universe loader."""

from __future__ import annotations

import json

import pytest


def test_load_from_json_file(tmp_path):
    from xenon.scanners._shared.universe import load_tickers_from_json

    path = tmp_path / "tickers.json"
    path.write_text(json.dumps(["AAPL", "MSFT", "GOOG"]))
    result = load_tickers_from_json(path)
    assert result == ["AAPL", "GOOG", "MSFT"]


def test_load_from_json_file_with_dict_rows(tmp_path):
    from xenon.scanners._shared.universe import load_tickers_from_json

    path = tmp_path / "tickers.json"
    path.write_text(json.dumps([{"ticker": "AAPL"}, {"ticker": "MSFT"}]))
    result = load_tickers_from_json(path)
    assert result == ["AAPL", "MSFT"]


def test_load_from_json_missing_file(tmp_path):
    from xenon.scanners._shared.universe import load_tickers_from_json

    result = load_tickers_from_json(tmp_path / "nonexistent.json")
    assert result == []


def test_dedup_and_normalize():
    from xenon.scanners._shared.universe import dedup_and_normalize

    tickers = ["aapl", "MSFT", "AAPL", "goog", "msft"]
    result = dedup_and_normalize(tickers)
    assert result == ["AAPL", "GOOG", "MSFT"]


def test_union_sources():
    from xenon.scanners._shared.universe import union_sources

    source_a = ["AAPL", "MSFT"]
    source_b = ["GOOG", "AAPL"]
    source_c = ["NVDA", "MSFT"]
    result = union_sources(source_a, source_b, source_c)
    assert result == ["AAPL", "GOOG", "MSFT", "NVDA"]


def test_union_sources_empty():
    from xenon.scanners._shared.universe import union_sources

    result = union_sources([], [], [])
    assert result == []
