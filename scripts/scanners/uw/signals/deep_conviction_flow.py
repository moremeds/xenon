"""Tier 1 signal: Deep Conviction Flow."""
from __future__ import annotations

from typing import Optional

from xenon.analysis.models import TickerData
from scripts.scanners.uw.models import SignalHit

MIN_PREMIUM = 500_000
MIN_ASK_SIDE = 0.80
MAX_MULTILEG = 0.10
MAX_MONEYNESS = 0.12
MIN_DTE = 6


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _alert_qualifies(alert: dict) -> bool:
    vol = _f(alert.get("volume"))
    oi = _f(alert.get("open_interest"))
    ask_side = _f(alert.get("ask_side_percent"))
    premium = _f(alert.get("total_premium"))
    multileg = _f(alert.get("multileg_percent"))
    moneyness = _f(alert.get("moneyness"))
    dte = _f(alert.get("expiry_dte") or alert.get("dte"))

    if vol is None or oi is None or vol <= oi:
        return False
    if ask_side is None or ask_side < MIN_ASK_SIDE:
        return False
    if premium is None or premium < MIN_PREMIUM:
        return False
    if multileg is not None and multileg >= MAX_MULTILEG:
        return False
    if moneyness is not None and abs(moneyness) > MAX_MONEYNESS:
        return False
    if dte is None or dte < MIN_DTE:
        return False
    return True


def detect(ticker: str, td: TickerData) -> Optional[SignalHit]:
    if not td.flow_alerts:
        return None
    if td.earnings_within_14d:
        return None

    qualifying = [a for a in td.flow_alerts if _alert_qualifies(a)]
    if not qualifying:
        return None

    total_premium = sum(_f(a.get("total_premium")) or 0 for a in qualifying)
    top = max(qualifying, key=lambda a: _f(a.get("total_premium")) or 0)

    premium_scale = min(total_premium / 2_000_000.0, 1.0)
    score = 0.5 + 0.5 * premium_scale

    return SignalHit(
        ticker=ticker,
        signal_type="deep_conviction_flow",
        tier=1,
        score=round(score, 3),
        evidence={
            "qualifying_alerts": len(qualifying),
            "total_premium": total_premium,
            "top_strike": top.get("strike"),
            "top_expiry": top.get("expiry"),
        },
        freshness="live",
    )
