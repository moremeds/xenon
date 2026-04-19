"""Tests for xenon.scanners.trend.universe — mirror-based loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_load_universe_from_mirror_returns_tickers(tmp_path: Path):
    from xenon.scanners.trend.universe import load_universe_from_mirror

    (tmp_path / "meta").mkdir()
    (tmp_path / "meta" / "universe.json").write_text(
        json.dumps(
            {
                "tickers": [
                    {"symbol": "AAPL", "marketCap": 3e12, "dollar_volume": 5e9, "tier": "mega_cap"},
                    {"symbol": "MSFT", "marketCap": 2e12, "dollar_volume": 4e9, "tier": "mega_cap"},
                ]
            }
        )
    )

    rows = load_universe_from_mirror(tmp_path)
    assert len(rows) == 2
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["marketCap"] == 3e12


def test_load_universe_raises_when_mirror_missing(tmp_path: Path):
    from xenon.scanners.trend.universe import load_universe_from_mirror

    with pytest.raises(FileNotFoundError):
        load_universe_from_mirror(tmp_path)


def test_load_universe_returns_empty_list_when_no_tickers(tmp_path: Path):
    """Behavior when the file exists but `tickers` is missing/empty."""
    from xenon.scanners.trend.universe import load_universe_from_mirror

    (tmp_path / "meta").mkdir()
    (tmp_path / "meta" / "universe.json").write_text(json.dumps({}))

    rows = load_universe_from_mirror(tmp_path)
    assert rows == []


def test_trendscanconfig_has_turnover_and_tier_floors():
    from xenon.scanners.trend.config import TrendScanConfig

    cfg = TrendScanConfig()
    assert cfg.min_turnover_rate == 0.0
    assert cfg.exclude_tiers == set()


def test_trendscanconfig_accepts_exclude_tiers():
    from xenon.scanners.trend.config import TrendScanConfig

    cfg = TrendScanConfig(exclude_tiers={"leveraged_etf", "inverse_etf"})
    assert "leveraged_etf" in cfg.exclude_tiers
