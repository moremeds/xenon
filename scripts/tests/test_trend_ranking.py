"""Tests for trend scanner ranking module."""

from __future__ import annotations

import pytest


def test_rank_by_final_score():
    from scripts.scanners.trend.ranking import rank_candidates

    from scripts.scanners.trend.models import TrendCandidate

    candidates = [
        TrendCandidate(
            ticker="AAPL", direction="bullish", final_score=0.7, scores={"trend": 0.8, "structure": 0.6}, spot_price=185
        ),
        TrendCandidate(
            ticker="NVDA",
            direction="bullish",
            final_score=0.9,
            scores={"trend": 0.95, "structure": 0.8},
            spot_price=148,
        ),
        TrendCandidate(
            ticker="GOOG", direction="bullish", final_score=0.5, scores={"trend": 0.6, "structure": 0.4}, spot_price=155
        ),
    ]
    ranked = rank_candidates(candidates, top_n=3)
    assert [c.ticker for c in ranked] == ["NVDA", "AAPL", "GOOG"]


def test_rank_top_n_limit():
    from scripts.scanners.trend.ranking import rank_candidates

    from scripts.scanners.trend.models import TrendCandidate

    candidates = [
        TrendCandidate(ticker=f"T{i}", direction="bullish", final_score=i * 0.1, scores={"trend": 0.5}, spot_price=100)
        for i in range(10)
    ]
    ranked = rank_candidates(candidates, top_n=3)
    assert len(ranked) == 3
    assert ranked[0].ticker == "T9"


def test_apply_min_thresholds_filters():
    from scripts.scanners.trend.ranking import apply_min_thresholds

    from scripts.scanners.trend.models import TrendCandidate

    candidates = [
        TrendCandidate(
            ticker="GOOD", direction="bullish", final_score=0.8, scores={"trend": 0.6, "structure": 0.5}, spot_price=100
        ),
        TrendCandidate(
            ticker="BAD_TREND",
            direction="bullish",
            final_score=0.7,
            scores={"trend": 0.35, "structure": 0.5},
            spot_price=100,
        ),
        TrendCandidate(
            ticker="BAD_STRUCT",
            direction="bullish",
            final_score=0.6,
            scores={"trend": 0.5, "structure": 0.2},
            spot_price=100,
        ),
    ]
    thresholds = {"trend": 0.4, "structure": 0.3}
    filtered = apply_min_thresholds(candidates, thresholds)
    assert len(filtered) == 1
    assert filtered[0].ticker == "GOOD"


def test_apply_min_thresholds_missing_score():
    from scripts.scanners.trend.ranking import apply_min_thresholds

    from scripts.scanners.trend.models import TrendCandidate

    candidates = [
        TrendCandidate(ticker="MISSING", direction="bullish", final_score=0.8, scores={"trend": 0.8}, spot_price=100),
    ]
    thresholds = {"trend": 0.4, "structure": 0.3}
    filtered = apply_min_thresholds(candidates, thresholds)
    assert len(filtered) == 0


def test_compute_final_scores():
    from scripts.scanners.trend.ranking import compute_final_score

    scores = {"trend": 0.9, "structure": 0.7, "volatility": 0.6, "flow": 0.8}
    weights = {"trend": 0.35, "structure": 0.25, "volatility": 0.20, "flow": 0.20}
    result = compute_final_score(scores, weights)
    expected = (0.9 * 0.35) + (0.7 * 0.25) + (0.6 * 0.20) + (0.8 * 0.20)
    assert abs(result - expected) < 1e-9


def test_mixed_directions_ranked_together():
    from scripts.scanners.trend.ranking import rank_candidates

    from scripts.scanners.trend.models import TrendCandidate

    candidates = [
        TrendCandidate(ticker="BULL", direction="bullish", final_score=0.8, scores={"trend": 0.8}, spot_price=100),
        TrendCandidate(ticker="BEAR", direction="bearish", final_score=0.9, scores={"trend": 0.9}, spot_price=100),
    ]
    ranked = rank_candidates(candidates, top_n=10)
    assert ranked[0].ticker == "BEAR"
    assert ranked[0].direction == "bearish"
