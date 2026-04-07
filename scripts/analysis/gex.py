"""GEX helpers: flip point, wall ranking, opex pinning detection."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional


def _gamma(row: dict) -> Optional[float]:
    for key in ("gamma", "net_gamma", "total_gamma", "value"):
        v = row.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def detect_flip_point(strikes: list[dict]) -> Optional[float]:
    sorted_strikes = sorted(
        (s for s in strikes if _gamma(s) is not None and s.get("strike") is not None),
        key=lambda s: float(s["strike"]),
    )
    if len(sorted_strikes) < 2:
        return None

    prev = sorted_strikes[0]
    for curr in sorted_strikes[1:]:
        prev_g = _gamma(prev)
        curr_g = _gamma(curr)
        if prev_g is None or curr_g is None:
            prev = curr
            continue
        if prev_g < 0 and curr_g >= 0:
            return (float(prev["strike"]) + float(curr["strike"])) / 2.0
        prev = curr
    return None


def rank_walls(strikes: list[dict], top_n: int = 3) -> list[dict]:
    scored = [
        {**s, "_abs_gamma": abs(_gamma(s) or 0.0)}
        for s in strikes
        if _gamma(s) is not None
    ]
    scored.sort(key=lambda s: s["_abs_gamma"], reverse=True)
    return [{k: v for k, v in s.items() if k != "_abs_gamma"} for s in scored[:top_n]]


def detect_pinning(
    strikes: list[dict],
    *,
    price: float,
    opex_week: bool,
    min_gamma: float = 1.0,
    max_distance_pct: float = 1.0,
) -> Optional[dict]:
    if not opex_week or price <= 0:
        return None

    for wall in rank_walls(strikes, top_n=5):
        strike = float(wall.get("strike") or 0)
        gamma = _gamma(wall) or 0.0
        if abs(gamma) < min_gamma:
            continue
        distance_pct = abs(strike - price) / price * 100.0
        if distance_pct <= max_distance_pct:
            return {
                "pin_strike": strike,
                "gamma": gamma,
                "distance_pct": distance_pct,
            }
    return None


def extract_call_wall(strikes: list[dict]) -> Optional[dict]:
    """Top *positive*-gamma strike (sign preserved — rank_walls cannot do this)."""
    candidates = [
        s for s in strikes if (_gamma(s) or 0.0) > 0 and s.get("strike") is not None
    ]
    if not candidates:
        return None
    top = max(candidates, key=lambda s: _gamma(s) or 0.0)
    return {"strike": float(top["strike"]), "gamma": float(_gamma(top) or 0.0)}


def extract_put_wall(strikes: list[dict]) -> Optional[dict]:
    """Top *negative*-gamma strike (sign preserved)."""
    candidates = [
        s for s in strikes if (_gamma(s) or 0.0) < 0 and s.get("strike") is not None
    ]
    if not candidates:
        return None
    top = min(candidates, key=lambda s: _gamma(s) or 0.0)
    return {"strike": float(top["strike"]), "gamma": float(_gamma(top) or 0.0)}


def compute_gamma_per_1pct(
    strikes: list[dict], price: Optional[float]
) -> Optional[float]:
    """Sum of |gamma| across strikes within ±1% of price.

    Approximates dealer hedging intensity for a 1% move. Returns None if price
    is missing/non-positive or no strikes fall in the band.
    """
    if not price or price <= 0:
        return None
    band = price * 0.01
    total = 0.0
    seen = False
    for s in strikes:
        g = _gamma(s)
        strike = s.get("strike")
        if g is None or strike is None:
            continue
        if abs(float(strike) - price) <= band:
            total += abs(g)
            seen = True
    return total if seen else None


def is_opex_week(today: date) -> bool:
    """True if today is within 3 calendar days before the 3rd Friday of the month."""
    first_day = today.replace(day=1)
    first_friday_offset = (4 - first_day.weekday()) % 7
    third_friday = first_day + timedelta(days=first_friday_offset + 14)
    delta = (third_friday - today).days
    return 0 <= delta <= 3
