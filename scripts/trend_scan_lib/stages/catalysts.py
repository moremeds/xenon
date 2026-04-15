"""Stage C catalyst detection — headlines + event flags.

Always degrades gracefully. If UW client is None or raises, returns
(empty list, 0.5) — catalyst information is informational, not
gate-forming."""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_BULLISH_TYPES = {
    "analyst_upgrade",
    "guidance_raise",
    "ma_rumor_acquirer_of",
    "activist_long",
    "fda_pdufa_positive",
}
_BEARISH_TYPES = {
    "analyst_downgrade",
    "guidance_cut",
    "activist_short",
    "fraud_allegation",
    "fda_pdufa_negative",
}
_NEUTRAL_TYPES = {"earnings_within_7d", "headline_momentum", "ma_rumor_target_of"}


def fetch_catalysts(
    *,
    ticker: str,
    direction: str,
    uw_client: Optional[object],
    earnings_days: int,
) -> tuple[list[str], float]:
    """Return (catalyst tags, score in [0,1]) for *ticker* in *direction*.

    Score convention:
      > 0.6  — catalysts align with direction
      0.5    — neutral (no catalysts, or event risk only)
      < 0.4  — catalysts oppose direction
    """
    catalysts: list[str] = []

    if 0 <= earnings_days <= 7:
        catalysts.append("earnings_within_7d")

    if uw_client is not None:
        try:
            headlines = uw_client.get_headlines(ticker) or []
        except Exception as exc:
            logger.warning("fetch_catalysts: UW headlines failed for %s: %s", ticker, exc)
            headlines = []

        for h in headlines:
            t = h.get("type")
            if t and t not in catalysts and (t in _BULLISH_TYPES or t in _BEARISH_TYPES or t in _NEUTRAL_TYPES):
                catalysts.append(t)

    aligned_set = _BULLISH_TYPES if direction == "bullish" else _BEARISH_TYPES
    opposed_set = _BEARISH_TYPES if direction == "bullish" else _BULLISH_TYPES
    aligned = sum(1 for c in catalysts if c in aligned_set)
    opposed = sum(1 for c in catalysts if c in opposed_set)

    if aligned == 0 and opposed == 0:
        return catalysts, 0.5
    raw = (aligned - opposed) / max(aligned + opposed, 1)
    score = 0.5 + raw / 2
    return catalysts, score
