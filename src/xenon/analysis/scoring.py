"""4-bucket composite scoring with reweighting and fast mode."""
from __future__ import annotations

from typing import Literal

from xenon.analysis.models import BucketScores, RegimeState, TickerData, VRPState


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
    """Weight budget: gex_sign ±8 + flip_relative ±6 + call_wall ±5 +
    put_wall ±5 + gamma_intensity ±4 = ±28 (matches BUCKET_WEIGHTS)."""
    score = 0.0

    # gex_sign tilt (±8)
    if regime.gex_sign == "positive":
        score += 8
    elif regime.gex_sign == "negative":
        score -= 8

    # flip relative (±6)
    if regime.gex_flip_relative == "below_price":
        score += 6
    elif regime.gex_flip_relative == "above_price":
        score -= 6

    # Wall geometry: closer wall = stronger pull. Use distance% from price.
    # Call wall ABOVE price acts as a ceiling (bearish); intensity scales with
    # closeness. Put wall BELOW price acts as a floor (bullish).
    price = td.price
    if price and price > 0:
        if td.call_wall_strike and td.call_wall_strike >= price:
            dist_pct = abs(td.call_wall_strike - price) / price * 100.0
            # 0% away = -5, 5%+ away = 0
            score -= max(0.0, 5.0 * (1.0 - min(dist_pct, 5.0) / 5.0))
        if td.put_wall_strike and td.put_wall_strike <= price:
            dist_pct = abs(price - td.put_wall_strike) / price * 100.0
            score += max(0.0, 5.0 * (1.0 - min(dist_pct, 5.0) / 5.0))

    # Gamma per 1% — high intensity reinforces the existing sign tilt (±4)
    if td.gamma_per_1pct is not None and td.gamma_per_1pct > 0:
        # log-ish scale: cap at 4
        intensity = min(4.0, td.gamma_per_1pct / 5.0)
        if regime.gex_sign == "positive":
            score += intensity
        elif regime.gex_sign == "negative":
            score -= intensity

    return max(-28.0, min(28.0, score))


def _score_volatility(td: TickerData, vrp: VRPState) -> float:
    """Weight budget: iv_rank ±8 + vrp_zscore ±8 + rv/iv_ratio ±6 +
    ts_inverted ±6 = ±28."""
    score = 0.0

    # IV rank tilt (±8). Prefer iv_rank (deep) over iv_percentile fallback.
    iv_signal = td.iv_rank if td.iv_rank is not None else vrp.iv_percentile
    if iv_signal is not None:
        if iv_signal > 75:
            score -= 8  # rich vol → fade vol-buying
        elif iv_signal > 60:
            score -= 4
        elif iv_signal < 25:
            score += 8  # cheap vol → favor long premium
        elif iv_signal < 40:
            score += 4

    # VRP z-score (±8)
    if vrp.vrp_zscore is not None:
        if vrp.vrp_zscore > 1.5:
            score += 8
        elif vrp.vrp_zscore > 0.5:
            score += 4
        elif vrp.vrp_zscore < -1.0:
            score -= 8
        elif vrp.vrp_zscore < 0:
            score -= 4

    # RV/IV ratio (±6) — RV outpacing IV = vol underpriced (bullish premium)
    if td.iv is not None and td.rv is not None and td.iv > 0:
        ratio = td.rv / td.iv
        if ratio > 1.1:
            score += 6
        elif ratio > 1.0:
            score += 3
        elif ratio < 0.7:
            score -= 6
        elif ratio < 0.85:
            score -= 3

    # Term structure inverted (±6)
    if vrp.ts_inverted is True:
        score -= 6

    return max(-28.0, min(28.0, score))


def _score_flow(td: TickerData) -> float:
    """Weight budget: net_premium tilt ±12 + short_volume_trend ±6 +
    alert_count fallback ±6 = ±24."""
    score = 0.0

    # Net premium tilt (±12) — primary signal when deep enrichment present
    ncp = td.net_call_premium
    npp = td.net_put_premium
    if ncp is not None and npp is not None:
        total = abs(ncp) + abs(npp)
        if total > 0:
            tilt = (ncp - npp) / total  # in [-1, 1]
            score += max(-12.0, min(12.0, tilt * 12.0))

    # Short volume trend (±6) — rising short ratio = bearish, falling = bullish
    trend = td.short_volume_trend
    if trend and len(trend) >= 2:
        # trend[0] is newest, trend[-1] is oldest (per fetcher contract)
        delta = trend[0] - trend[-1]
        if delta > 0.05:
            score -= 6
        elif delta > 0.02:
            score -= 3
        elif delta < -0.05:
            score += 6
        elif delta < -0.02:
            score += 3

    # Alert count fallback (±6) — only when no net_premium signal available
    if (ncp is None or npp is None) and td.flow_alerts:
        n = len(td.flow_alerts)
        base = min(6.0, n * 0.75)
        if td.pcr is not None:
            if td.pcr > 1.5:
                score -= base  # heavy puts
            elif td.pcr < 0.5:
                score += base  # heavy calls
        else:
            score += base * 0.5  # weak positive bias on activity alone

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
    # Fast mode skips flow + positioning so available_count is at most 2,
    # which forces _grade() to return B or C — no explicit cap needed.

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
