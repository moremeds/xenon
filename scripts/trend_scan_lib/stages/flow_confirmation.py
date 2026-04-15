"""Stage C: Flow confirmation scoring -- institutional participation alignment."""

from __future__ import annotations

from scripts.scanner_lib.scoring import normalize_score

FLOW_WEIGHTS = {
    "ask_dominance": 0.20,
    "flow_repetition": 0.25,
    "expiry_clustering": 0.15,
    "strike_reasonableness": 0.15,
    "delta_vega": 0.25,
}


def score_ask_dominance(ratio: float) -> float:
    if ratio >= 0.80:
        return 1.0
    if ratio >= 0.60:
        return 0.7
    if ratio >= 0.50:
        return 0.5
    return 0.2


def score_flow_repetition(count: int) -> float:
    if count >= 3:
        return 1.0
    if count == 2:
        return 0.6
    if count == 1:
        return 0.2
    return 0.0


def score_expiry_clustering(*, cluster_ratio: float) -> float:
    if cluster_ratio >= 0.7:
        return 1.0
    if cluster_ratio >= 0.5:
        return 0.7
    if cluster_ratio >= 0.3:
        return 0.4
    return 0.2


def score_strike_reasonableness(*, avg_strike_pct_otm: float) -> float:
    if avg_strike_pct_otm <= 0.05:
        return 1.0
    if avg_strike_pct_otm <= 0.10:
        return 0.7
    if avg_strike_pct_otm <= 0.15:
        return 0.4
    return 0.2


def score_delta_vega_flow(*, net_delta: float, net_vega: float) -> float:
    if net_delta > 0 and net_vega > 0:
        return 1.0
    if net_delta > 0:
        return 0.7
    if net_delta == 0 and net_vega == 0:
        return 0.5
    return 0.1


def score_dark_pool_alignment(*, dp_direction: str) -> float:
    if dp_direction.lower() == "bullish":
        return 0.15
    if dp_direction.lower() == "bearish":
        return -0.05
    return 0.0


def compute_flow_score(data: dict, *, direction: str = "bullish") -> float:
    scores = {
        "ask_dominance": score_ask_dominance(data.get("ask_dominance", 0.5)),
        "flow_repetition": score_flow_repetition(data.get("flow_count", 0)),
        "expiry_clustering": score_expiry_clustering(cluster_ratio=data.get("expiry_cluster_ratio", 0.5)),
        "strike_reasonableness": score_strike_reasonableness(avg_strike_pct_otm=data.get("avg_strike_pct_otm", 0.10)),
        "delta_vega": score_delta_vega_flow(
            net_delta=data.get("net_delta", 0),
            net_vega=data.get("net_vega", 0),
        ),
    }
    composite = sum(scores[k] * w for k, w in FLOW_WEIGHTS.items())
    dp_bonus = score_dark_pool_alignment(dp_direction=data.get("dp_direction", "neutral"))
    return normalize_score(composite + dp_bonus)
