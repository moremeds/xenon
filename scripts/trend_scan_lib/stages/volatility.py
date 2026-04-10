"""Stage B addon: Volatility state scoring and trade type suggestion."""

from __future__ import annotations

from scripts.scanner_lib.scoring import normalize_score

VOL_WEIGHTS = {"iv_rank": 0.40, "term_structure": 0.30, "iv_rv_ratio": 0.30}


def score_iv_rank(iv_rank: float) -> float:
    if iv_rank < 30:
        return 1.0
    if iv_rank < 50:
        return 0.7
    if iv_rank < 75:
        return 0.4
    return 0.2


def score_term_structure(shape: str) -> float:
    shapes = {"normal": 1.0, "flat": 0.6, "inverted": 0.3}
    return shapes.get(shape.lower(), 0.5)


def score_iv_rv_ratio(ratio: float) -> float:
    if ratio == 0:
        return 0.5
    if ratio <= 0.8:
        return 1.0
    if ratio <= 1.0:
        return 0.8
    if ratio <= 1.2:
        return 0.5
    if ratio <= 1.5:
        return 0.3
    return 0.1


def compute_vol_score(data: dict) -> tuple[float, list[str]]:
    flags: list[str] = []
    scores = {
        "iv_rank": score_iv_rank(data.get("iv_rank", 50)),
        "term_structure": score_term_structure(data.get("term_structure", "flat")),
        "iv_rv_ratio": score_iv_rv_ratio(data.get("iv_rv_ratio", 1.0)),
    }
    earnings_days = data.get("earnings_days")
    if earnings_days is not None and earnings_days <= 14:
        if data.get("iv_rank", 0) >= 50 or data.get("term_structure") == "inverted":
            flags.append("event_premium")
    composite = sum(scores[k] * w for k, w in VOL_WEIGHTS.items())
    return normalize_score(composite), flags


def suggest_trade_type(*, iv_rank: float, term_structure: str, capped: bool) -> str:
    if iv_rank >= 60 and capped:
        return "premium_sell"
    if iv_rank >= 30 and capped:
        return "call_spread"
    if iv_rank < 30 and term_structure.lower() == "normal":
        return "debit_call"
    if iv_rank < 30:
        return "debit_call"
    return "call_spread"
