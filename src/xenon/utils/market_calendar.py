"""The one US-equity market calendar.

Regular session only: 09:30 (inclusive) to 16:00 (exclusive), America/New_York,
Monday to Friday, minus the holidays in ``src/xenon/config/market_holidays.json``.

Every Python "is the market open" question goes through ``is_open`` so that
holiday and timezone behaviour cannot drift between Python callers. The web
``useMarketHours`` hook and ``scripts/infra/ib_realtime/ib_realtime_server.js``
have their own checks.

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
_CLOSE_INCLUSIVE_MINUTE = time(16, 1)

# src/xenon/utils/market_calendar.py → src/xenon/config/market_holidays.json
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "market_holidays.json"


class CalendarYearMissing(LookupError):
    """The holiday file is unreadable or has no entry for the requested year."""


@lru_cache(maxsize=1)
def _load_file() -> dict[str, frozenset[date]]:
    """Read and validate the holiday file: ``{"YYYY": ["YYYY-MM-DD", ...]}``.

    Any unreadable or malformed content raises ``CalendarYearMissing`` so that
    lenient callers take the logged weekday fallback instead of crashing.
    """
    try:
        with open(_CONFIG_PATH) as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("top level must be an object of year -> [dates]")
        parsed: dict[str, frozenset[date]] = {}
        for year, entries in raw.items():
            if not isinstance(year, str) or not year.isdigit():
                raise ValueError(f"bad year key {year!r}")
            if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
                raise ValueError(f"{year}: must be a list of ISO date strings")
            days = frozenset(date.fromisoformat(e) for e in entries)
            if any(d.year != int(year) for d in days):
                raise ValueError(f"{year}: contains a date from another year")
            parsed[year] = days
        return parsed
    except (OSError, ValueError, TypeError) as exc:
        raise CalendarYearMissing(f"cannot read {_CONFIG_PATH}: {exc}") from exc


def _holidays(year: int) -> frozenset[date]:
    days = _load_file().get(str(year))
    if days is None:
        raise CalendarYearMissing(
            f"{_CONFIG_PATH.name} has no entry for {year}; add the {year} NYSE holidays"
        )
    return days


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


def is_open(now: datetime, *, strict: bool = True, close_inclusive_minute: bool = False) -> bool:
    """True when the regular session is open at *now* (09:30 <= t < 16:00 ET).

    ``now`` must be timezone-aware; any zone is converted to ET.
    ``strict=False`` turns a missing or malformed holiday file into a logged
    error plus the weekday-only answer instead of raising.
    ``close_inclusive_minute=True`` keeps the whole 16:00 minute open
    (t < 16:01): the snapshot-staleness checks use it so the stricter
    market-open threshold applies through 16:00:59.
    """
    et = _to_et(now)
    try:
        trading = is_trading_day(et.date())
    except CalendarYearMissing as exc:
        if strict:
            raise
        logger.error("%s; falling back to weekday-only market hours", exc)
        trading = et.weekday() < 5
    close = _CLOSE_INCLUSIVE_MINUTE if close_inclusive_minute else MARKET_CLOSE
    return trading and MARKET_OPEN <= et.time() < close


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


def _hm(mins: int) -> str:
    return f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"


def _status(et: datetime) -> str:
    """Human-readable status line for the CLI."""
    if is_open(et):
        mins = int((datetime.combine(et.date(), MARKET_CLOSE, tzinfo=ET) - et).total_seconds() / 60)
        return f"OPEN ({_hm(mins)} to close)"
    if et.weekday() >= 5:
        return "CLOSED (weekend)"
    if not is_trading_day(et.date()):
        return "CLOSED (holiday)"
    if et.time() < MARKET_OPEN:
        mins = int((datetime.combine(et.date(), MARKET_OPEN, tzinfo=ET) - et).total_seconds() / 60)
        return f"CLOSED (pre-market, {_hm(mins)} to open)"
    return "CLOSED (after hours)"


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: print current market status."""
    argparse.ArgumentParser(prog="xenon-market-hours", description="Print the current US market status.").parse_args(
        argv
    )
    et = now_et()
    print(f"Current time (ET): {et.strftime('%A %Y-%m-%d %H:%M:%S %Z')}")
    print(f"Market status: {_status(et)}")
    print(f"is_market_open(): {is_open(et)}")
    print(f"Last market close: {last_close(et).strftime('%Y-%m-%d %H:%M %Z')}")


if __name__ == "__main__":
    main()
