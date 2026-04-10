"""Tests for trend scanner config."""

from __future__ import annotations

import pytest


def test_default_config():
    from scripts.trend_scan_lib.config import TrendScanConfig

    cfg = TrendScanConfig()
    assert cfg.top_n == 25
    assert cfg.max_workers == 15
    assert cfg.weights == {"trend": 0.35, "structure": 0.25, "volatility": 0.20, "flow": 0.20}
    assert abs(sum(cfg.weights.values()) - 1.0) < 0.01


def test_config_min_thresholds():
    from scripts.trend_scan_lib.config import TrendScanConfig

    cfg = TrendScanConfig()
    assert cfg.min_thresholds["trend"] == 0.4
    assert cfg.min_thresholds["structure"] == 0.3


def test_config_universe_floor():
    from scripts.trend_scan_lib.config import TrendScanConfig

    cfg = TrendScanConfig()
    assert cfg.min_market_cap == 1_000_000_000
    assert cfg.min_dollar_volume == 10_000_000
    assert cfg.min_price == 5.0


def test_config_custom_top_n():
    from scripts.trend_scan_lib.config import TrendScanConfig

    cfg = TrendScanConfig(top_n=10)
    assert cfg.top_n == 10
