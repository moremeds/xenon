"""Shared market calendar utilities.

Provides holiday data, market-open checks, and trading-day calculations.
Holidays are loaded from scripts/config/market_holidays.json.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "market_holidays.json"

# Module-level cache so we only read the file once per process.
_holidays_cache: dict = {}


def _load_holidays_file() -> dict:
    """Load the holidays JSON file (cached)."""
    if not _holidays_cache:
        try:
            with open(_CONFIG_PATH) as f:
                data = json.load(f)
            _holidays_cache.update(data)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    return _holidays_cache


def load_holidays(year: int = None) -> set:
    """Return a set of holiday date-strings for the given year.

    Args:
        year: 4-digit year.  Defaults to the current year.

    Returns:
        Set of "YYYY-MM-DD" strings.  Empty set if the year is not configured.
    """
    if year is None:
        year = datetime.now().year
    data = _load_holidays_file()
    dates = data.get(str(year), [])
    return set(dates)


def is_market_open(now: datetime = None) -> bool:
    """Check whether the US equity market is open at the given moment.

    Rules:
        - Monday through Friday only
        - 9:30 AM -- 4:00 PM Eastern (naive datetime assumed ET)
        - Not a configured holiday

    Args:
        now: datetime to check.  Defaults to ``datetime.now()``.
    """
    if now is None:
        now = datetime.now()

    # Weekend check
    if now.weekday() >= 5:
        return False

    # Holiday check
    date_str = now.strftime("%Y-%m-%d")
    holidays = load_holidays(now.year)
    if date_str in holidays:
        return False

    # Time window: 9:30 -- 16:00
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now < market_open or now > market_close:
        return False

    return True


def get_last_n_trading_days(n: int, from_date: datetime = None,
                           include_today: bool = False) -> list:
    """Return the last *n* trading days as ``["YYYY-MM-DD", ...]``.

    A trading day is a weekday that is not a holiday.
    If the market hasn't closed yet today (before 4 PM) or today is not a
    trading day, counting starts from the previous day.

    Args:
        n: Number of trading days to return.
        from_date: Reference datetime.  Defaults to ``datetime.now()``.
        include_today: If True and today is a trading day, always include
            today as the first entry even if the market hasn't closed yet.
            Use this for evaluations that need intraday data.
    """
    if from_date is None:
        from_date = datetime.now()

    trading_days: list = []
    current = from_date

    # If include_today and today is a trading day, add it first
    if include_today and _is_trading_day(current):
        trading_days.append(current.strftime("%Y-%m-%d"))

    # If today isn't eligible yet, step back one day
    if not _is_trading_day(current) or current.hour < 16:
        current -= timedelta(days=1)

    while len(trading_days) < n:
        if _is_trading_day(current):
            day_str = current.strftime("%Y-%m-%d")
            if day_str not in trading_days:  # avoid duplicate if include_today
                trading_days.append(day_str)
        current -= timedelta(days=1)

        # Safety: avoid infinite loops for misconfigured calendars
        if not trading_days and (from_date - current).days > 14:
            break

    return trading_days


# ── internal helpers ──────────────────────────────────────────────────

def _is_trading_day(dt: datetime) -> bool:
    """Check if *dt* falls on a trading day (weekday + not holiday)."""
    if dt.weekday() >= 5:
        return False
    date_str = dt.strftime("%Y-%m-%d")
    holidays = load_holidays(dt.year)
    return date_str not in holidays
