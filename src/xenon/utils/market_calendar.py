"""The one US-equity market calendar.

Regular session: 09:30 (inclusive) to 16:00 (exclusive), America/New_York,
Monday to Friday, minus the holidays in ``src/xenon/config/market_holidays.json``.
On the early-close days in ``src/xenon/config/market_early_closes.json`` the
session ends at 13:00. ``us_equity_session`` adds the stock/ETF extended and
overnight sessions.

Every Python "is the market open" question goes through ``is_open`` so that
holiday and timezone behaviour cannot drift between Python callers. The web
``useMarketHours`` hook and ``scripts/infra/ib_realtime/ib_realtime_server.js``
have their own checks.

* Times are always ET-aware. A naive ``datetime`` is rejected (``ValueError``);
  the host timezone is never consulted.
* A year that is missing from the holiday file raises ``CalendarYearMissing``.
  ``is_open(..., strict=False)`` is for callers that must not crash: it logs an
  error and falls back to the weekday-only rule for that year.
* Both files list NYSE dates (nyse.com/markets/hours-calendars). The two files
  must cover the same years; a year missing from either raises
  ``CalendarYearMissing``.
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
EARLY_CLOSE = time(13, 0)
PRE_MARKET_OPEN = time(4, 0)
AFTER_HOURS_CLOSE = time(20, 0)
EARLY_AFTER_HOURS_CLOSE = time(17, 0)  # NYSE late sessions close at 17:00 on early-close days
OVERNIGHT_OPEN = time(20, 0)  # Blue Ocean overnight, Sun-Thu 20:00 -> next day 03:50
OVERNIGHT_CLOSE = time(3, 50)

# src/xenon/utils/market_calendar.py → src/xenon/config/market_holidays.json
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "market_holidays.json"
_EARLY_CLOSE_PATH = _CONFIG_PATH.with_name("market_early_closes.json")


class CalendarYearMissing(LookupError):
    """The holiday file is unreadable or has no entry for the requested year."""


@lru_cache(maxsize=1)
def _load_file() -> dict[str, frozenset[date]]:
    """Read and validate the holiday file: ``{"YYYY": ["YYYY-MM-DD", ...]}``.

    Any unreadable or malformed content raises ``CalendarYearMissing`` so that
    lenient callers take the logged weekday fallback instead of crashing.
    """
    return _parse_date_file(_CONFIG_PATH)


@lru_cache(maxsize=1)
def _load_early_closes() -> dict[str, frozenset[date]]:
    """Read and validate the early-close file (same format as the holiday file)."""
    return _parse_date_file(_EARLY_CLOSE_PATH)


def _parse_date_file(path: Path) -> dict[str, frozenset[date]]:
    try:
        with open(path) as f:
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
        raise CalendarYearMissing(f"cannot read {path}: {exc}") from exc


def _holidays(year: int) -> frozenset[date]:
    days = _load_file().get(str(year))
    if days is None:
        raise CalendarYearMissing(
            f"{_CONFIG_PATH.name} has no entry for {year}; add the {year} NYSE holidays"
        )
    return days


def session_close(d: date) -> time:
    """Regular-session close on *d*: 13:00 on an early-close day, else 16:00."""
    days = _load_early_closes().get(str(d.year))
    if days is None:
        raise CalendarYearMissing(
            f"{_EARLY_CLOSE_PATH.name} has no entry for {d.year}; add the {d.year} NYSE early closes"
        )
    return EARLY_CLOSE if d in days else MARKET_CLOSE


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
    """True when the regular session is open at *now* (09:30 <= t < close ET).

    The close is ``session_close`` (16:00, or 13:00 on an early-close day).

    ``now`` must be timezone-aware; any zone is converted to ET.
    ``strict=False`` turns a missing or malformed holiday file into a logged
    error plus the weekday-only answer instead of raising.
    ``close_inclusive_minute=True`` keeps the whole 16:00 minute open
    (t < close + 1 min): the snapshot-staleness checks use it so the stricter
    market-open threshold applies through the whole close minute.
    """
    et = _to_et(now)
    try:
        trading = is_trading_day(et.date())
        close = session_close(et.date())
    except CalendarYearMissing as exc:
        if strict:
            raise
        logger.error("%s; falling back to weekday-only market hours", exc)
        trading = et.weekday() < 5
        close = MARKET_CLOSE
    if close_inclusive_minute:
        close = time(close.hour, close.minute + 1)
    return trading and MARKET_OPEN <= et.time() < close


def last_close(now: datetime) -> datetime:
    """Most recent regular-session close at or before *now* (ET-aware)."""
    et = _to_et(now)
    d = et.date()
    if not (is_trading_day(d) and et.time() >= session_close(d)):
        d -= timedelta(days=1)
        while not is_trading_day(d):
            d -= timedelta(days=1)
    return datetime.combine(d, session_close(d), tzinfo=ET)


def us_equity_session(now: datetime) -> str:
    """Session of a US stock/ETF at *now*: ``rth``, ``extended``, ``overnight`` or ``closed``.

    ``extended`` = pre-market 04:00-09:30 and after-hours close-20:00 (17:00 on
    an early-close day). ``overnight`` = Sun-Thu 20:00 -> next day 03:50, when
    that next day is a trading day.
    """
    # ponytail: holiday handling of the overnight session is inferred (no session
    # before a closed day), not checked against Blue Ocean's calendar.
    et = _to_et(now)
    d, t = et.date(), et.time()
    if t >= OVERNIGHT_OPEN or t < OVERNIGHT_CLOSE:
        session_day = d + timedelta(days=1) if t >= OVERNIGHT_OPEN else d
        start_weekday = (session_day - timedelta(days=1)).weekday()
        return "overnight" if start_weekday in (6, 0, 1, 2, 3) and is_trading_day(session_day) else "closed"
    if not is_trading_day(d):
        return "closed"
    close = session_close(d)
    if MARKET_OPEN <= t < close:
        return "rth"
    after_close = EARLY_AFTER_HOURS_CLOSE if close == EARLY_CLOSE else AFTER_HOURS_CLOSE
    if PRE_MARKET_OPEN <= t < MARKET_OPEN or close <= t < after_close:
        return "extended"
    return "closed"


def _to_et(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("market calendar needs a timezone-aware datetime, got a naive one")
    return now.astimezone(ET)


def _hm(mins: int) -> str:
    return f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"


def _status(et: datetime) -> str:
    """Human-readable status line for the CLI."""
    if is_open(et):
        mins = int((datetime.combine(et.date(), session_close(et.date()), tzinfo=ET) - et).total_seconds() / 60)
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
