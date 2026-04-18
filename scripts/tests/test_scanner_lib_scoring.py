"""Tests for scanner_lib scoring utilities."""

from __future__ import annotations

import pytest


def test_weighted_composite_basic():
    from scanners._shared.scoring import weighted_composite

    scores = {"trend": 0.8, "structure": 0.6, "vol": 0.5, "flow": 0.7}
    weights = {"trend": 0.35, "structure": 0.25, "vol": 0.20, "flow": 0.20}
    result = weighted_composite(scores, weights)
    expected = (0.8 * 0.35) + (0.6 * 0.25) + (0.5 * 0.20) + (0.7 * 0.20)
    assert abs(result - expected) < 1e-9


def test_weighted_composite_missing_score_treated_as_zero():
    from scanners._shared.scoring import weighted_composite

    scores = {"trend": 0.8}
    weights = {"trend": 0.35, "structure": 0.65}
    result = weighted_composite(scores, weights)
    assert abs(result - 0.8 * 0.35) < 1e-9


def test_weighted_composite_weights_must_sum_to_one():
    from scanners._shared.scoring import weighted_composite

    scores = {"a": 0.5}
    weights = {"a": 0.5, "b": 0.3}
    with pytest.raises(ValueError, match="weights must sum to 1.0"):
        weighted_composite(scores, weights)


def test_passes_min_thresholds_all_pass():
    from scanners._shared.scoring import passes_min_thresholds

    scores = {"trend": 0.6, "structure": 0.5}
    thresholds = {"trend": 0.4, "structure": 0.3}
    assert passes_min_thresholds(scores, thresholds) is True


def test_passes_min_thresholds_one_fails():
    from scanners._shared.scoring import passes_min_thresholds

    scores = {"trend": 0.35, "structure": 0.5}
    thresholds = {"trend": 0.4, "structure": 0.3}
    assert passes_min_thresholds(scores, thresholds) is False


def test_passes_min_thresholds_missing_score_fails():
    from scanners._shared.scoring import passes_min_thresholds

    scores = {"trend": 0.6}
    thresholds = {"trend": 0.4, "structure": 0.3}
    assert passes_min_thresholds(scores, thresholds) is False


def test_normalize_score_clamps():
    from scanners._shared.scoring import normalize_score

    assert normalize_score(1.5) == 1.0
    assert normalize_score(-0.3) == 0.0
    assert normalize_score(0.5) == 0.5


def test_normalize_score_nan_returns_zero():
    from scanners._shared.scoring import normalize_score

    assert normalize_score(float("nan")) == 0.0
    assert normalize_score(float("inf")) == 0.0
    assert normalize_score(float("-inf")) == 0.0
