"""Tests for trend scanner universe builder."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def test_static_source_loads(tmp_path):
    from scripts.trend_scan_lib.universe import build_static_universe

    sp = tmp_path / "sp500.json"
    nq = tmp_path / "nasdaq100.json"
    sp.write_text(json.dumps(["AAPL", "MSFT", "GOOG"]))
    nq.write_text(json.dumps(["AAPL", "NVDA", "TSLA"]))

    result = build_static_universe(sp500_path=sp, nasdaq100_path=nq)
    assert result == ["AAPL", "GOOG", "MSFT", "NVDA", "TSLA"]


def test_static_source_missing_file(tmp_path):
    from scripts.trend_scan_lib.universe import build_static_universe

    result = build_static_universe(
        sp500_path=tmp_path / "missing.json",
        nasdaq100_path=tmp_path / "also_missing.json",
    )
    assert result == []


def test_uw_flow_source_extracts_tickers():
    from scripts.trend_scan_lib.universe import build_uw_flow_universe

    mock_client = MagicMock()
    mock_client.get_flow_alerts.return_value = [
        {"ticker": "AAPL", "premium": 500_000},
        {"ticker": "NVDA", "premium": 200_000},
        {"ticker": "AAPL", "premium": 300_000},
    ]
    mock_client.get_darkpool_flow.return_value = [
        {"ticker": "TSLA", "volume": 1_000_000},
    ]

    result = build_uw_flow_universe(client=mock_client, min_premium=100_000, lookback_days=5)
    assert "AAPL" in result
    assert "NVDA" in result
    assert "TSLA" in result


def test_uw_flow_source_handles_error():
    from scripts.trend_scan_lib.universe import build_uw_flow_universe

    mock_client = MagicMock()
    mock_client.get_flow_alerts.side_effect = Exception("API down")
    mock_client.get_darkpool_flow.side_effect = Exception("API down")

    result = build_uw_flow_universe(client=mock_client, min_premium=100_000, lookback_days=5)
    assert result == []


def test_ib_scanner_source_extracts_tickers():
    from scripts.trend_scan_lib.universe import build_ib_scanner_universe

    mock_client = MagicMock()
    mock_client.run_scanner.side_effect = [
        [{"ticker": "AAPL"}, {"ticker": "AMD"}],
        [{"ticker": "NVDA"}, {"ticker": "AAPL"}],
    ]

    result = build_ib_scanner_universe(client=mock_client)
    assert sorted(result) == ["AAPL", "AMD", "NVDA"]


def test_ib_scanner_source_handles_error():
    from scripts.trend_scan_lib.universe import build_ib_scanner_universe

    mock_client = MagicMock()
    mock_client.run_scanner.side_effect = Exception("IB Gateway down")

    result = build_ib_scanner_universe(client=mock_client)
    assert result == []


def test_build_full_universe(tmp_path):
    from scripts.trend_scan_lib.config import TrendScanConfig
    from scripts.trend_scan_lib.universe import build_universe

    sp = tmp_path / "sp500.json"
    nq = tmp_path / "nasdaq100.json"
    sp.write_text(json.dumps(["AAPL", "MSFT"]))
    nq.write_text(json.dumps(["NVDA"]))

    cfg = TrendScanConfig(sp500_path=str(sp), nasdaq100_path=str(nq))

    with (
        patch("scripts.trend_scan_lib.universe.build_uw_flow_universe", return_value=["GOOG"]),
        patch("scripts.trend_scan_lib.universe.build_ib_scanner_universe", return_value=["TSLA"]),
    ):
        result = build_universe(cfg, uw_client=MagicMock(), ib_client=MagicMock())

    assert result == ["AAPL", "GOOG", "MSFT", "NVDA", "TSLA"]
