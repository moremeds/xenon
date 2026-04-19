"""Candidate construction and deterministic ranking."""
from __future__ import annotations

from typing import Optional

from scripts.scanners.uw.confluence import compute_confluence, is_type_f
from scripts.scanners.uw.models import ContextFlag, ScanCandidate, SignalHit

RANKING_TIER_WEIGHTS = {1: 3.0, 2: 1.5}
RAW_RANKING_EXCLUDE: frozenset[str] = frozenset({"dark_pool_accumulation"})


def build_candidate(
    ticker: str,
    hits: list[SignalHit],
    context_flags: list[ContextFlag],
) -> Optional[ScanCandidate]:
    non_dp_hits = [h for h in hits if h.signal_type not in RAW_RANKING_EXCLUDE]
    if not non_dp_hits:
        return None

    raw_score = sum(
        h.score * RANKING_TIER_WEIGHTS.get(h.tier, 0.0)
        for h in non_dp_hits
    )
    confluence = compute_confluence(hits)
    type_f = is_type_f(hits)
    final_score = raw_score + confluence
    return ScanCandidate(
        ticker=ticker.upper(),
        hits=hits,
        context_flags=context_flags,
        raw_score=raw_score,
        confluence_score=confluence,
        final_score=final_score,
        is_type_f=type_f,
    )


def rank_candidates(candidates: list[ScanCandidate]) -> list[ScanCandidate]:
    return sorted(
        candidates,
        key=lambda c: (not c.is_type_f, -c.final_score, c.ticker),
    )
