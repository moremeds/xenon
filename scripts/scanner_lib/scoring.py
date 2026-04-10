"""Scoring utilities shared across scanners."""

from __future__ import annotations


def weighted_composite(scores: dict[str, float], weights: dict[str, float]) -> float:
    """Compute weighted composite score. Weights must sum to 1.0. Missing scores treated as 0."""
    total_weight = sum(weights.values())
    if abs(total_weight - 1.0) > 0.01:
        raise ValueError(f"weights must sum to 1.0, got {total_weight:.3f}")
    return sum(scores.get(k, 0.0) * w for k, w in weights.items())


def passes_min_thresholds(scores: dict[str, float], thresholds: dict[str, float]) -> bool:
    """Check that every threshold key has a score >= the threshold value."""
    return all(scores.get(k, 0.0) >= v for k, v in thresholds.items())


def normalize_score(value: float) -> float:
    """Clamp a value to [0.0, 1.0]."""
    return max(0.0, min(1.0, value))
