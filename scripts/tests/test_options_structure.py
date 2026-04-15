"""Tests for Stage B options structure scoring."""

from __future__ import annotations

import pytest


def test_score_gamma_flip_above():
    from scripts.trend_scan_lib.stages.options_structure import score_gamma_flip

    assert score_gamma_flip(spot=150, gamma_flip=145) == 1.0


def test_score_gamma_flip_at():
    from scripts.trend_scan_lib.stages.options_structure import score_gamma_flip

    assert score_gamma_flip(spot=145, gamma_flip=145) == 0.5


def test_score_gamma_flip_below():
    from scripts.trend_scan_lib.stages.options_structure import score_gamma_flip

    assert score_gamma_flip(spot=140, gamma_flip=145) == 0.2


def test_score_gamma_flip_zero():
    from scripts.trend_scan_lib.stages.options_structure import score_gamma_flip

    assert score_gamma_flip(spot=150, gamma_flip=0) == 0.5


def test_score_call_wall_far():
    from scripts.trend_scan_lib.stages.options_structure import score_call_wall_distance

    assert score_call_wall_distance(spot=100, call_wall=110) == 1.0


def test_score_call_wall_close():
    from scripts.trend_scan_lib.stages.options_structure import score_call_wall_distance

    assert score_call_wall_distance(spot=100, call_wall=101) < 0.4


def test_score_call_wall_zero():
    from scripts.trend_scan_lib.stages.options_structure import score_call_wall_distance

    assert score_call_wall_distance(spot=100, call_wall=0) == 0.5


def test_score_put_wall_nearby():
    from scripts.trend_scan_lib.stages.options_structure import score_put_wall_support

    assert score_put_wall_support(spot=100, put_wall=98) > 0.7


def test_score_put_wall_far():
    from scripts.trend_scan_lib.stages.options_structure import score_put_wall_support

    assert score_put_wall_support(spot=100, put_wall=90) < 0.4


def test_score_max_pain_above():
    from scripts.trend_scan_lib.stages.options_structure import score_max_pain

    assert score_max_pain(spot=150, max_pain=145) > 0.7


def test_score_max_pain_pinned():
    from scripts.trend_scan_lib.stages.options_structure import score_max_pain

    result = score_max_pain(spot=145, max_pain=145)
    assert 0.3 <= result <= 0.5


def test_score_oi_change_bullish():
    from scripts.trend_scan_lib.stages.options_structure import score_oi_change

    assert score_oi_change(net_call_oi_change=5000, net_put_oi_change=-2000) == 1.0


def test_score_oi_change_bearish():
    from scripts.trend_scan_lib.stages.options_structure import score_oi_change

    assert score_oi_change(net_call_oi_change=-3000, net_put_oi_change=5000) < 0.3


def test_score_net_gex_positive():
    from scripts.trend_scan_lib.stages.options_structure import score_net_gex

    assert score_net_gex(net_gex=500_000) > 0.7


def test_score_net_gex_negative():
    from scripts.trend_scan_lib.stages.options_structure import score_net_gex

    assert score_net_gex(net_gex=-500_000) < 0.3


def test_pinning_reject_severe():
    from scripts.trend_scan_lib.stages.options_structure import is_severely_pinned

    assert is_severely_pinned(spot=100, max_pain=100.3, gex_at_spot=1_000_000, spot_pct_threshold=0.005) is True


def test_pinning_reject_not_pinned():
    from scripts.trend_scan_lib.stages.options_structure import is_severely_pinned

    assert is_severely_pinned(spot=105, max_pain=100, gex_at_spot=100_000, spot_pct_threshold=0.005) is False


def test_compute_structure_score_bullish():
    from scripts.trend_scan_lib.stages.options_structure import compute_structure_score

    data = {
        "spot": 150,
        "gamma_flip": 145,
        "call_wall": 165,
        "put_wall": 146,
        "max_pain": 148,
        "net_gex": 200_000,
        "net_call_oi_change": 3000,
        "net_put_oi_change": -1000,
        "gex_at_spot": 50_000,
    }
    score, rejected = compute_structure_score(data)
    assert not rejected
    assert score > 0.6


def test_compute_structure_score_rejected_pinning():
    from scripts.trend_scan_lib.stages.options_structure import compute_structure_score

    data = {
        "spot": 100,
        "gamma_flip": 95,
        "call_wall": 110,
        "put_wall": 95,
        "max_pain": 100.2,
        "net_gex": 100_000,
        "net_call_oi_change": 0,
        "net_put_oi_change": 0,
        "gex_at_spot": 2_000_000,
    }
    score, rejected = compute_structure_score(data)
    assert rejected


def test_compute_structure_score_rejects_overhead_wall_without_support():
    """A large call wall within 2% above spot with no put wall below
    = immediate overhead resistance with nothing to bounce off.
    Must hard-reject like pinning does."""
    from scripts.trend_scan_lib.stages.options_structure import compute_structure_score

    score, rejected = compute_structure_score(
        {
            "spot": 100.0,
            "max_pain": 95.0,  # not pinned
            "gex_at_spot": 0.0,
            "call_wall": 101.5,  # within 2% above spot
            "put_wall": 0.0,  # no support below
            "net_call_oi_change": 0,
            "net_put_oi_change": 0,
            "net_gex": 0,
            "gamma_flip": 95.0,
        }
    )

    assert rejected is True, "overhead wall with no put support must reject"
    assert score == 0.0


def test_compute_structure_score_overhead_wall_ok_with_put_support():
    """Overhead wall is acceptable if a meaningful put wall exists below —
    range-bound structure is still tradeable."""
    from scripts.trend_scan_lib.stages.options_structure import compute_structure_score

    score, rejected = compute_structure_score(
        {
            "spot": 100.0,
            "max_pain": 95.0,
            "gex_at_spot": 0.0,
            "call_wall": 101.5,
            "put_wall": 98.0,  # meaningful support
            "net_call_oi_change": 0,
            "net_put_oi_change": 1000,
            "net_gex": 0,
            "gamma_flip": 98.0,
        }
    )

    assert rejected is False
