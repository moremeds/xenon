"""4-bucket composite scoring with reweighting and fast mode."""
from __future__ import annotations

from typing import Literal

from scripts.analysis.models import BucketScores, RegimeState, TickerData, VRPState


BUCKET_WEIGHTS: dict[str, int] = {
    "market_structure": 28,
    "volatility": 28,
    "flow": 24,
    "positioning": 20,
}

Mode = Literal["full", "fast"]


def score_to_bias(composite: float) -> str:
    if composite >= 60:
        return "STRONGLY_BULLISH"
    if composite >= 20:
        return "BULLISH"
    if composite > -20:
        return "MIXED"
    if composite > -60:
        return "BEARISH"
    return "STRONGLY_BEARISH"


def _score_market_structure(td: TickerData, regime: RegimeState) -> float:
    score = 0.0
    if regime.gex_sign == "positive":
        score += 10
    elif regime.gex_sign == "negative":
        score -= 10
    if regime.gex_flip_relative == "below_price":
        score += 8
    elif regime.gex_flip_relative == "above_price":
        score -= 8
    return max(-28.0, min(28.0, score))


def _score_volatility(td: TickerData, vrp: VRPState) -> float:
    score = 0.0
    if vrp.iv_percentile is not None:
        if vrp.iv_percentile > 75:
            score -= 6
        elif vrp.iv_percentile < 30:
            score += 6
    if vrp.vrp_zscore is not None:
        if vrp.vrp_zscore > 1.0:
            score += 8
        elif vrp.vrp_zscore < 0:
            score -= 8
    if vrp.ts_inverted is True:
        score -= 10
    return max(-28.0, min(28.0, score))


def _score_flow(td: TickerData) -> float:
    if not td.flow_alerts:
        return 0.0
    n = len(td.flow_alerts)
    score = min(12.0, n * 1.5)
    if td.pcr is not None:
        if td.pcr > 1.5:
            score += 8
        elif td.pcr > 1.2:
            score += 4
        elif td.pcr < 0.5:
            score -= 6
    return max(-24.0, min(24.0, score))


def _score_positioning(td: TickerData) -> float:
    """v1 LIMITATION: positioning bucket returns 0 — OI history not persisted."""
    return 0.0


def _grade(available_buckets: int, has_confluence: bool) -> Literal["A", "B", "C"]:
    if available_buckets >= 3 and has_confluence:
        return "A"
    if available_buckets >= 2:
        return "B"
    return "C"


def score_buckets(
    td: TickerData, vrp: VRPState, regime: RegimeState, *, mode: Mode = "full"
) -> BucketScores:
    raw: dict[str, float] = {}
    skipped: list[str] = []

    if td.bucket_available("market_structure"):
        raw["market_structure"] = _score_market_structure(td, regime)
    else:
        skipped.append("market_structure")
        raw["market_structure"] = 0.0

    if td.bucket_available("volatility"):
        raw["volatility"] = _score_volatility(td, vrp)
    else:
        skipped.append("volatility")
        raw["volatility"] = 0.0

    if mode == "fast":
        raw["flow"] = 0.0
        if "flow" not in skipped:
            skipped.append("flow")
    elif td.bucket_available("flow"):
        raw["flow"] = _score_flow(td)
    else:
        skipped.append("flow")
        raw["flow"] = 0.0

    if mode == "fast":
        raw["positioning"] = 0.0
        if "positioning" not in skipped:
            skipped.append("positioning")
    elif td.bucket_available("positioning"):
        raw["positioning"] = _score_positioning(td)
    else:
        skipped.append("positioning")
        raw["positioning"] = 0.0

    available_max = sum(
        w for name, w in BUCKET_WEIGHTS.items() if name not in skipped
    )
    reweighted = bool(skipped)
    if available_max <= 0:
        composite = 0.0
    else:
        raw_sum = sum(raw[name] for name in BUCKET_WEIGHTS if name not in skipped)
        composite = raw_sum * (100.0 / available_max)

    composite = max(-100.0, min(100.0, composite))
    bias = score_to_bias(composite)

    available_count = sum(1 for name in BUCKET_WEIGHTS if name not in skipped)
    has_confluence = abs(composite) >= 40
    grade = _grade(available_count, has_confluence)
    if mode == "fast" and grade == "A":
        grade = "B"

    return BucketScores(
        market_structure=raw["market_structure"],
        volatility=raw["volatility"],
        flow=raw["flow"],
        positioning=raw["positioning"],
        composite=composite,
        grade=grade,
        bias=bias,  # type: ignore[arg-type]
        mode=mode,
        reweighted=reweighted,
        skipped_buckets=skipped,
    )
