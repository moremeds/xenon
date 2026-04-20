"""VRP state builder and regime classifier (R0/R1/R2).

Pure functions: take a TickerData, return a VRPState or RegimeState.
No I/O. No stateful fallbacks. If VRP history is unavailable, vrp_zscore
is None and the regime classifier biases toward R1 (never R0).
"""
from __future__ import annotations

import statistics
from typing import Optional

from xenon.analysis.models import RegimeState, TickerData, VRPState


def _parse_iv(raw) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(raw) * 100.0
    except (TypeError, ValueError):
        return None


def _compute_ts_ratio(term_structure: list[dict]) -> tuple[Optional[float], Optional[bool]]:
    parsed: list[tuple[int, float]] = []
    for row in term_structure:
        dte = row.get("dte") or row.get("days") or row.get("DTE")
        iv = _parse_iv(row.get("iv") or row.get("IV"))
        if dte is None or iv is None:
            continue
        try:
            parsed.append((int(dte), iv))
        except (TypeError, ValueError):
            continue

    near_candidates = [p for p in parsed if p[0] > 7]
    if len(near_candidates) < 2:
        return None, None

    near = min(near_candidates, key=lambda p: p[0])
    far = min(parsed, key=lambda p: abs(p[0] - 90))

    if near == far or far[1] == 0:
        return None, None

    ratio = near[1] / far[1]
    return ratio, ratio > 1.05


def _zscore(history: list[float], current: float) -> Optional[float]:
    if not history or len(history) < 10:
        return None
    try:
        mean = statistics.mean(history)
        std = statistics.stdev(history) if len(history) > 1 else 0.0
    except statistics.StatisticsError:
        return None
    return (current - mean) / max(std, 0.01)


def build_vrp_state(td: TickerData) -> VRPState:
    vrp_raw = None
    if td.iv is not None and td.rv is not None:
        vrp_raw = td.iv - td.rv

    vrp_zscore = None
    if vrp_raw is not None and td.vrp_history:
        vrp_zscore = _zscore(td.vrp_history, vrp_raw)

    ts_ratio, ts_inverted = (None, None)
    if td.term_structure:
        ts_ratio, ts_inverted = _compute_ts_ratio(td.term_structure)

    if td.iv is None:
        freshness = "unavailable"
    elif td.vrp_history is None or vrp_zscore is None:
        freshness = "stale"
    else:
        freshness = "live"

    return VRPState(
        vrp_raw=vrp_raw,
        vrp_zscore=vrp_zscore,
        iv_percentile=td.iv_percentile,
        ts_ratio=ts_ratio,
        ts_inverted=ts_inverted,
        earnings_within_14d=td.earnings_within_14d,
        data_freshness=freshness,
    )


def _net_gex(gex: dict) -> Optional[float]:
    if not gex:
        return None
    for key in ("net", "net_gamma", "total", "value"):
        v = gex.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _flip_distance(gex: dict, price: Optional[float]) -> tuple[Optional[float], Optional[str]]:
    if not gex or price is None or price == 0:
        return None, None
    flip = gex.get("flip") or gex.get("flip_point") or gex.get("gamma_flip")
    if flip is None:
        return None, None
    try:
        signed = (float(flip) - price) / price * 100.0
    except (TypeError, ValueError):
        return None, None
    magnitude = abs(signed)
    if signed > 0.5:
        relative = "above_price"
    elif signed < -0.5:
        relative = "below_price"
    else:
        relative = "at_price"
    return magnitude, relative


def classify_regime(td: TickerData, vrp: VRPState) -> RegimeState:
    net_gex = _net_gex(td.gex) if td.gex else None
    flip_dist, gex_flip_relative = (
        _flip_distance(td.gex, td.price) if td.gex else (None, None)
    )

    if net_gex is None:
        gex_sign = None
    elif net_gex > 0:
        gex_sign = "positive"
    elif net_gex < 0:
        gex_sign = "negative"
    else:
        gex_sign = "mixed"

    if vrp.ts_inverted is True and vrp.vrp_zscore is not None and vrp.vrp_zscore < 0:
        return RegimeState(
            regime="R2", reason="Term structure inverted + VRP negative",
            gex_sign=gex_sign, gex_flip_relative=gex_flip_relative,
            flip_distance_pct=flip_dist,
        )

    if (
        net_gex is not None and net_gex < 0
        and flip_dist is not None and flip_dist > 2.0
        and (vrp.vrp_zscore is None or vrp.vrp_zscore < 0.3)
    ):
        return RegimeState(
            regime="R2", reason="Deeply negative GEX + thin/unknown VRP",
            gex_sign=gex_sign, gex_flip_relative=gex_flip_relative,
            flip_distance_pct=flip_dist,
        )

    if vrp.ts_inverted is True or (
        vrp.vrp_zscore is not None and vrp.vrp_zscore < 0.3
    ):
        reason = "Caution: inverted TS" if vrp.ts_inverted else "Caution: thin VRP"
        return RegimeState(
            regime="R1", reason=reason,
            gex_sign=gex_sign, gex_flip_relative=gex_flip_relative,
            flip_distance_pct=flip_dist,
        )

    if (
        gex_flip_relative == "below_price"
        and vrp.vrp_zscore is not None and vrp.vrp_zscore > 0.5
    ):
        return RegimeState(
            regime="R0", reason="Positive GEX + elevated VRP",
            gex_sign=gex_sign, gex_flip_relative=gex_flip_relative,
            flip_distance_pct=flip_dist,
        )

    return RegimeState(
        regime="R1", reason="Mixed signals",
        gex_sign=gex_sign, gex_flip_relative=gex_flip_relative,
        flip_distance_pct=flip_dist,
    )
