"""Market-hours gate for the option chain snapshotter.

Uses exchange_calendars (XNYS schedule) to determine whether we are inside
the extended window:  SESSION_PRE_OPEN_MIN before open through
SESSION_POST_CLOSE_MIN after close on RTH days.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import exchange_calendars as ec
import pandas as pd

from .config import SESSION_POST_CLOSE_MIN, SESSION_PRE_OPEN_MIN

_nyse: ec.ExchangeCalendar | None = None


def _calendar() -> ec.ExchangeCalendar:
    global _nyse
    if _nyse is None:
        _nyse = ec.get_calendar("XNYS")
    return _nyse


def is_session_open(now: datetime | None = None) -> bool:
    """Return True if we are within the snapshotter's active window.

    The window extends SESSION_PRE_OPEN_MIN before RTH open and
    SESSION_POST_CLOSE_MIN after RTH close so the first and last prints
    of the day are captured.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)
    cal = _calendar()

    ts_utc = pd.Timestamp(now).tz_convert("UTC")
    # is_session() needs a timezone-naive date string / Timestamp.
    date_naive = pd.Timestamp(ts_utc.date())

    if not cal.is_session(date_naive):
        return False
    try:
        open_t = cal.session_open(date_naive)  # returns UTC-aware pd.Timestamp
        close_t = cal.session_close(date_naive)
    except Exception:
        return False

    window_open = open_t - pd.Timedelta(minutes=SESSION_PRE_OPEN_MIN)
    window_close = close_t + pd.Timedelta(minutes=SESSION_POST_CLOSE_MIN)
    return window_open <= ts_utc <= window_close


def next_session_open(now: datetime | None = None) -> datetime:
    """Return the start of the next active window (±buffer) as a UTC datetime."""
    if now is None:
        now = datetime.now(tz=timezone.utc)
    cal = _calendar()

    ts_utc = pd.Timestamp(now).tz_convert("UTC")
    end_search = ts_utc + pd.Timedelta(days=7)

    # sessions_in_range needs timezone-naive dates.
    future_sessions = cal.sessions_in_range(
        pd.Timestamp(ts_utc.date()),
        pd.Timestamp(end_search.date()),
    )
    for session in future_sessions:
        try:
            open_t = cal.session_open(session)
        except Exception:
            continue
        window_open = open_t - pd.Timedelta(minutes=SESSION_PRE_OPEN_MIN)
        if window_open > ts_utc:
            return window_open.to_pydatetime().astimezone(timezone.utc)

    return now + timedelta(hours=24)
