"""Tier 1 signal: GEX Pinning (mega-caps during opex week)."""
from __future__ import annotations

from datetime import date
from typing import Optional

from xenon.analysis.gex import detect_pinning, is_opex_week
from xenon.analysis.models import TickerData
from xenon.scanners.uw.models import SignalHit

MEGA_CAPS: frozenset[str] = frozenset({
    "SPY", "QQQ", "IWM", "DIA",
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA",
})

MIN_GAMMA = 1.0


def detect(ticker: str, td: TickerData, *, today: Optional[date] = None) -> Optional[SignalHit]:
    if ticker.upper() not in MEGA_CAPS:
        return None
    if td.gex_by_strike is None or td.price is None:
        return None

    check_date = today or date.today()
    if not is_opex_week(check_date):
        return None

    strikes = td.gex_by_strike.get("strikes") if isinstance(td.gex_by_strike, dict) else None
    if not strikes:
        return None

    pin = detect_pinning(strikes, price=td.price, opex_week=True, min_gamma=MIN_GAMMA)
    if pin is None:
        return None

    distance_score = max(0.0, 1.0 - pin["distance_pct"])
    gamma_score = min(1.0, abs(pin["gamma"]) / 10.0)
    score = 0.5 * distance_score + 0.5 * gamma_score

    return SignalHit(
        ticker=ticker.upper(),
        signal_type="gex_pinning",
        tier=1,
        score=round(score, 3),
        evidence=pin,
        freshness="live",
    )
