"""Equity / equity-option RTH helpers.

No holiday calendar in V1 (SL §Market Hours: weekday holidays are the known
gap). Adding a calendar is a follow-up.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

_NYC = ZoneInfo("America/New_York")
_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)


def is_rth(now: datetime) -> bool:
    nyc = now.astimezone(_NYC)
    if nyc.weekday() >= 5:
        return False
    return _RTH_OPEN <= nyc.time() < _RTH_CLOSE


def is_opt_tradeable(now: datetime) -> bool:
    """Equity options: RTH only, weekdays. SL §7 Market-hours bullet."""
    return is_rth(now)
