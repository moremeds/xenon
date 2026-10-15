"""NYSE RTH gating for the option-chain snapshotter.

Pure function — no IO, no DB, no env. Returns True only when the supplied
UTC instant falls inside an NYSE regular-session minute. Closed days
(weekends, exchange holidays) and early-close days (e.g. day after
Thanksgiving, July 3 when the next day is a holiday) are honored via
`exchange_calendars.get_calendar("XNYS")`.

The launchd agent fires every 600s 24/7; this gate is what keeps the
overnight/weekend ticks from writing guaranteed-failing rows into
`archive.snapshot_run`.
"""

from __future__ import annotations

from datetime import datetime

import exchange_calendars as xcals

_NYSE = xcals.get_calendar("XNYS")


def is_nyse_rth(ts_utc: datetime) -> bool:
    """Return True iff `ts_utc` is inside an NYSE regular-trading-hours minute.

    Args:
        ts_utc: tz-aware UTC instant to test.

    Raises:
        ValueError: if `ts_utc` is naive (tzinfo is None).
    """
    if ts_utc.tzinfo is None:
        raise ValueError("ts_utc must be tz-aware (got naive datetime)")
    # is_trading_minute treats the close minute as outside the session, which
    # matches IB market-data behavior (no ticks at 16:00:00 ET sharp). For
    # early-close days, the calendar returns the day's actual close minute,
    # so July 3 at 13:00 ET (next day = July 4 holiday) is honored
    # automatically.
    return bool(_NYSE.is_trading_minute(ts_utc))
