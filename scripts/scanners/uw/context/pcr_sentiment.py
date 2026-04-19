"""PCR sentiment context flag (zero weight in ranking)."""
from __future__ import annotations

from typing import Optional

from scripts.analysis.models import TickerData
from scripts.scanners.uw.models import ContextFlag


def flag(ticker: str, td: TickerData) -> Optional[ContextFlag]:
    if td.pcr is None:
        return None
    if td.earnings_within_14d:
        return None

    pcr = td.pcr
    if pcr > 1.5:
        label = "Extreme Fear"
    elif pcr > 1.2:
        label = "Elevated Fear"
    elif pcr < 0.5:
        label = "Complacent"
    else:
        return None

    return ContextFlag(
        ticker=ticker.upper(),
        layer="pcr_sentiment",
        label=label,
        value=pcr,
    )
