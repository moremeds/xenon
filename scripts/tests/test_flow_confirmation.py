"""Tests for Stage C flow confirmation scoring."""

from __future__ import annotations

import pytest


def test_score_ask_dominance_high():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_ask_dominance

    assert score_ask_dominance(0.85) == 1.0


def test_score_ask_dominance_moderate():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_ask_dominance

    assert score_ask_dominance(0.65) == 0.7


def test_score_ask_dominance_low():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_ask_dominance

    assert score_ask_dominance(0.45) == 0.2


def test_score_flow_repetition_multiple():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_flow_repetition

    assert score_flow_repetition(5) == 1.0


def test_score_flow_repetition_single():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_flow_repetition

    assert score_flow_repetition(1) == 0.2


def test_score_flow_repetition_zero():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_flow_repetition

    assert score_flow_repetition(0) == 0.0


def test_score_expiry_clustering_tight():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_expiry_clustering

    assert score_expiry_clustering(cluster_ratio=0.8) == 1.0


def test_score_expiry_clustering_scattered():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_expiry_clustering

    assert score_expiry_clustering(cluster_ratio=0.3) == 0.4


def test_score_strike_reasonableness_near():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_strike_reasonableness

    assert score_strike_reasonableness(avg_strike_pct_otm=0.05) == 1.0


def test_score_strike_reasonableness_far():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_strike_reasonableness

    assert score_strike_reasonableness(avg_strike_pct_otm=0.20) == 0.2


def test_score_delta_vega_positive():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_delta_vega_flow

    assert score_delta_vega_flow(net_delta=50_000, net_vega=30_000) == 1.0


def test_score_delta_vega_contradictory():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_delta_vega_flow

    assert score_delta_vega_flow(net_delta=-20_000, net_vega=-10_000) == 0.1


def test_score_dark_pool_bullish_bonus():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_dark_pool_alignment

    assert score_dark_pool_alignment(dp_direction="bullish") == 0.15


def test_score_dark_pool_none():
    from scripts.trend_scan_lib.stages.flow_confirmation import score_dark_pool_alignment

    assert score_dark_pool_alignment(dp_direction="neutral") == 0.0


def test_compute_flow_score_strong():
    from scripts.trend_scan_lib.stages.flow_confirmation import compute_flow_score

    data = {
        "ask_dominance": 0.85,
        "flow_count": 5,
        "expiry_cluster_ratio": 0.8,
        "avg_strike_pct_otm": 0.04,
        "net_delta": 40_000,
        "net_vega": 20_000,
        "dp_direction": "bullish",
    }
    score = compute_flow_score(data)
    assert score > 0.8


def test_compute_flow_score_weak():
    from scripts.trend_scan_lib.stages.flow_confirmation import compute_flow_score

    data = {
        "ask_dominance": 0.40,
        "flow_count": 1,
        "expiry_cluster_ratio": 0.2,
        "avg_strike_pct_otm": 0.25,
        "net_delta": -5_000,
        "net_vega": -3_000,
        "dp_direction": "neutral",
    }
    score = compute_flow_score(data)
    assert score < 0.3


def test_compute_flow_score_no_data():
    from scripts.trend_scan_lib.stages.flow_confirmation import compute_flow_score

    score = compute_flow_score({})
    assert 0.0 <= score <= 0.5
