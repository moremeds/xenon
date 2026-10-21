"""The one US-equity market calendar.

Regular session only: 09:30 (inclusive) to 16:00 (exclusive), America/New_York,
Monday to Friday, minus the holidays in ``src/xenon/config/market_holidays.json``.

Every "is the market open" question in the codebase goes through ``is_open`` so
that holiday and timezone behaviour cannot drift between callers.

* Times are always ET-aware. A naive ``datetime`` is rejected (``ValueError``);
  the host timezone is never consulted.
* A year that is missing from the holiday file raises ``CalendarYearMissing``.
  ``is_open(..., strict=False)`` is for callers that must not crash: it logs an
  error and falls back to the weekday-only rule for that year.
* The file lists full-day closures only. Early closes (13:00 ET) are not
  modelled; the session is treated as open until 16:00 on those days.
"""

import argparse
import json
import logging
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# src/xenon/utils/market_calendar.py → src/xenon/config/market_holidays.json
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "market_holidays.json"


class CalendarYearMissing(LookupError):
    """The holiday file is unreadable or has no entry for the requested year."""


@lru_cache(maxsize=1)
def _load_file() -> dict[str, list[str]]:
    with open(_CONFIG_PATH) as f:
        return json.load(f)


@lru_cache(maxsize=None)
def _holidays(year: int) -> frozenset[date]:
    try:
        entries = _load_file().get(str(year))
    except (OSError, ValueError) as exc:
        raise CalendarYearMissing(f"cannot read {_CONFIG_PATH}: {exc}") from exc
    if entries is None:
        raise CalendarYearMissing(
            f"{_CONFIG_PATH.name} has no entry for {year}; add the {year} NYSE holidays"
        )
    return frozenset(date.fromisoformat(s) for s in entries)


def now_et() -> datetime:
    """Current time as an ET-aware datetime."""
    return datetime.now(ET)


def current_session_date_et() -> date:
    """Current US-Eastern calendar date.

    Used by performance / nav_history paths so same-day rows always land on the
    same date regardless of host timezone.
    """
    return now_et().date()


def is_trading_day(d: date) -> bool:
    """True when *d* is a weekday that is not a configured holiday.

    Raises ``CalendarYearMissing`` when the year is not in the holiday file,
    even for a weekend date, so a stale file is noticed early.
    """
    holidays = _holidays(d.year)
    return d.weekday() < 5 and d not in holidays


def is_open(now: datetime, *, strict: bool = True) -> bool:
    """True when the regular session is open at *now* (09:30 <= t < 16:00 ET).

    ``now`` must be timezone-aware; any zone is converted to ET.
    ``strict=False`` turns a missing holiday year into a logged error plus the
    weekday-only answer instead of raising.
    """
    et = _to_et(now)
    try:
        trading = is_trading_day(et.date())
    except CalendarYearMissing as exc:
        if strict:
            raise
        logger.error("%s; falling back to weekday-only market hours", exc)
        trading = et.weekday() < 5
    return trading and MARKET_OPEN <= et.time() < MARKET_CLOSE


def last_close(now: datetime) -> datetime:
    """Most recent regular-session close at or before *now* (ET-aware)."""
    et = _to_et(now)
    d = et.date()
    if not (is_trading_day(d) and et.time() >= MARKET_CLOSE):
        d -= timedelta(days=1)
        while not is_trading_day(d):
            d -= timedelta(days=1)
    return datetime.combine(d, MARKET_CLOSE, tzinfo=ET)


def _to_et(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("market calendar needs a timezone-aware datetime, got a naive one")
    return now.astimezone(ET)


def _status(et: datetime) -> str:
    """Human-readable status line for the CLI."""
    if is_open(et):
        mins = int((datetime.combine(et.date(), MARKET_CLOSE, tzinfo=ET) - et).total_seconds() // 60)
        return f"OPEN ({mins // 60}h {mins % 60}m to close)"
    if et.weekday() >= 5:
        return "CLOSED (weekend)"
    if not is_trading_day(et.date()):
        return "CLOSED (holiday)"
    if et.time() < MARKET_OPEN:
        return "CLOSED (pre-market)"
    return "CLOSED (after hours)"


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: print current market status."""
    argparse.ArgumentParser(prog="xenon-market-hours", description="Print the current US market status.").parse_args(
        argv
    )
    et = now_et()
    print(f"Current time (ET): {et.strftime('%A %Y-%m-%d %H:%M:%S %Z')}")
    print(f"Market status: {_status(et)}")
    print(f"is_open(): {is_open(et)}")
    print(f"Last market close: {last_close(et).strftime('%Y-%m-%d %H:%M %Z')}")


if __name__ == "__main__":
    main()
