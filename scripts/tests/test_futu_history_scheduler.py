"""M9 — Futu history scheduler.

Pure scheduling helpers that the lifespan loop in server.py composes:

  - next_run_at_et(now_et, hour=16, minute=30): next future occurrence
    of HH:MM ET on a US weekday. Skips Sat/Sun.

The full asyncio loop itself (`futu_history_loop`) is a thin wrapper:
it computes `next_run_at_et()`, sleeps until then, runs the sync,
and loops. Tested via mocked-clock smoke; the scheduling logic is the
risk surface.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from xenon.api.services.futu_history_scheduler import next_run_at_et

ET = ZoneInfo("America/New_York")


def test_before_target_same_day():
    """Wednesday 10:00 ET → same day at 16:30 ET."""
    now = datetime(2026, 6, 3, 10, 0, tzinfo=ET)
    nxt = next_run_at_et(now)
    assert nxt == datetime(2026, 6, 3, 16, 30, tzinfo=ET)


def test_after_target_advances_to_next_weekday():
    """Wednesday 18:00 ET → Thursday 16:30 ET."""
    now = datetime(2026, 6, 3, 18, 0, tzinfo=ET)
    nxt = next_run_at_et(now)
    assert nxt == datetime(2026, 6, 4, 16, 30, tzinfo=ET)


def test_friday_after_target_skips_weekend():
    """Friday 18:00 ET → Monday 16:30 ET."""
    now = datetime(2026, 6, 5, 18, 0, tzinfo=ET)  # Fri
    nxt = next_run_at_et(now)
    assert nxt == datetime(2026, 6, 8, 16, 30, tzinfo=ET)  # Mon


def test_saturday_skips_to_monday():
    now = datetime(2026, 6, 6, 12, 0, tzinfo=ET)  # Sat
    nxt = next_run_at_et(now)
    assert nxt == datetime(2026, 6, 8, 16, 30, tzinfo=ET)


def test_sunday_skips_to_monday():
    now = datetime(2026, 6, 7, 12, 0, tzinfo=ET)  # Sun
    nxt = next_run_at_et(now)
    assert nxt == datetime(2026, 6, 8, 16, 30, tzinfo=ET)


def test_exactly_at_target_advances_one_minute_then_next_weekday():
    """At 16:30 exactly → next occurrence is the FOLLOWING weekday's 16:30,
    so a tick already firing doesn't get scheduled again at the same moment."""
    now = datetime(2026, 6, 3, 16, 30, tzinfo=ET)  # Wed
    nxt = next_run_at_et(now)
    assert nxt == datetime(2026, 6, 4, 16, 30, tzinfo=ET)


def test_custom_hour_minute():
    now = datetime(2026, 6, 3, 8, 0, tzinfo=ET)
    nxt = next_run_at_et(now, hour=14, minute=0)
    assert nxt == datetime(2026, 6, 3, 14, 0, tzinfo=ET)
