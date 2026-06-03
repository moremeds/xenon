"""Period-string → date resolver for /performance.

Pure function — no DB, no env. The caller resolves inception separately
(typically ``min(nav_history.date)`` for the scope) and passes it in.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

SUPPORTED_PERIODS: tuple[str, ...] = ("1M", "3M", "YTD", "All")

# Case-insensitive lookup → canonical token.
_NORMALIZE = {p.lower(): p for p in SUPPORTED_PERIODS}


class InvalidPeriodError(ValueError):
    """Raised when ``period`` is not in :data:`SUPPORTED_PERIODS` (case-insensitive)."""


def _normalize(period: str) -> str:
    key = period.strip().lower()
    if key not in _NORMALIZE:
        raise InvalidPeriodError(f"period must be one of {SUPPORTED_PERIODS!r}, got {period!r}")
    return _NORMALIZE[key]


def resolve_period_start(period: str, *, as_of: date, inception: Optional[date]) -> date:
    """Map a period token to a concrete start date.

    1M / 3M:  30 / 90 calendar days back from ``as_of`` (clamped to inception
              so we never look earlier than the scope's first NAV row).
    YTD:      Jan 1 of ``as_of.year``.
    All:      ``inception`` if known; falls back to YTD when inception is None
              (no NAV exists earlier anyway — safe default for cold-start scopes).
    """
    p = _normalize(period)
    if p == "YTD":
        candidate = date(as_of.year, 1, 1)
    elif p == "1M":
        candidate = as_of - timedelta(days=30)
    elif p == "3M":
        candidate = as_of - timedelta(days=90)
    elif p == "All":
        return inception if inception is not None else date(as_of.year, 1, 1)
    else:
        # Defensive — _normalize already raises on unknown tokens, but a
        # future maintainer adding to SUPPORTED_PERIODS will hit this.
        raise InvalidPeriodError(f"unhandled period: {p!r}")

    if inception is not None and candidate < inception:
        return inception
    return candidate
