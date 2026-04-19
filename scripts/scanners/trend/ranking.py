"""Trend scanner ranking — composite scoring, min thresholds, final sort."""

from __future__ import annotations

from scanners._shared.scoring import passes_min_thresholds, weighted_composite
from scripts.scanners.trend.models import TrendCandidate


def compute_final_score(scores: dict[str, float], weights: dict[str, float]) -> float:
    return weighted_composite(scores, weights)


def apply_min_thresholds(candidates: list[TrendCandidate], thresholds: dict[str, float]) -> list[TrendCandidate]:
    return [c for c in candidates if passes_min_thresholds(c.scores, thresholds)]


def rank_candidates(candidates: list[TrendCandidate], *, top_n: int = 25) -> list[TrendCandidate]:
    sorted_candidates = sorted(candidates, key=lambda c: (-c.final_score, c.ticker))
    return sorted_candidates[:top_n]
