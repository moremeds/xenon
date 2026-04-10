"""Tests for trend scanner models."""

from __future__ import annotations

import pytest


def test_trend_candidate_creation():
    from scripts.trend_scan_lib.models import TrendCandidate

    c = TrendCandidate(
        ticker="NVDA",
        direction="bullish",
        final_score=0.82,
        scores={"trend": 0.91, "structure": 0.75, "volatility": 0.68, "flow": 0.85},
        spot_price=148.30,
        indicators={"ma_20": 142.50, "rsi": 62.3},
        suggested_trade="debit_call",
        invalidation=142.50,
        holding_window="5-15 trading days",
    )
    assert c.ticker == "NVDA"
    assert c.spot_price == 148.30
    assert c.indicators["rsi"] == 62.3
    assert c.suggested_trade == "debit_call"


def test_trend_candidate_defaults():
    from scripts.trend_scan_lib.models import TrendCandidate

    c = TrendCandidate(
        ticker="AAPL",
        direction="bullish",
        final_score=0.5,
        scores={"trend": 0.5},
        spot_price=185.0,
    )
    assert c.indicators == {}
    assert c.suggested_trade == ""
    assert c.invalidation == 0.0
    assert c.holding_window == "5-15 trading days"
    assert c.flags == []
    assert c.summaries == {}


def test_trend_candidate_to_dict():
    from scripts.trend_scan_lib.models import TrendCandidate

    c = TrendCandidate(
        ticker="AAPL",
        direction="bullish",
        final_score=0.7,
        scores={"trend": 0.8},
        spot_price=185.0,
        indicators={"rsi": 60.0},
    )
    d = c.to_dict()
    assert d["ticker"] == "AAPL"
    assert d["spot_price"] == 185.0
    assert d["indicators"]["rsi"] == 60.0
    assert isinstance(d, dict)
