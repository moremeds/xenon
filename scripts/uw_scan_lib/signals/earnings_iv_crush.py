"""Tier 1 signal: Earnings IV Crush."""
from __future__ import annotations

from typing import Optional

from scripts.analysis.models import TickerData
from scripts.uw_scan_lib.models import SignalHit

MIN_IV_PCTL = 75.0


def detect(ticker: str, td: TickerData) -> Optional[SignalHit]:
    if td.iv_percentile is None or td.iv_percentile < MIN_IV_PCTL:
        return None
    if td.earnings_date is None:
        return None
    if not td.earnings_within_14d:
        return None

    score = min(1.0, (td.iv_percentile - MIN_IV_PCTL) / 25.0 + 0.5)

    return SignalHit(
        ticker=ticker.upper(),
        signal_type="earnings_iv_crush",
        tier=1,
        score=round(score, 3),
        evidence={
            "iv_percentile": td.iv_percentile,
            "earnings_date": str(td.earnings_date),
        },
        freshness="live",
    )
