"""Pins the assumed Futu `create_time` interpretation: naive
`YYYY-MM-DD HH:MM:SS` strings in America/New_York.

If a future operator probe (Task 5 Step 0) reveals Futu emits UTC instead,
update `_parse_ny_dt` per the plan's alternative branch — this test pins
the current contract so the switch is loud.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from xenon.clients.futu_history_fetcher import _parse_ny_dt


def test_parse_ny_dt_returns_aware_in_ny():
    dt = _parse_ny_dt("2024-05-01 10:30:00")
    assert dt.tzinfo is not None
    # Equivalent offset to America/New_York at the same wall-clock instant
    assert dt.tzinfo.utcoffset(dt) == ZoneInfo("America/New_York").utcoffset(dt)


def test_parse_ny_dt_wall_clock_matches():
    dt = _parse_ny_dt("2024-12-15 14:32:11")
    assert dt.year == 2024
    assert dt.month == 12
    assert dt.day == 15
    assert dt.hour == 14
    assert dt.minute == 32
    assert dt.second == 11


def test_parse_ny_dt_handles_dst_summer_offset():
    # July 2024 → EDT (-04:00)
    dt = _parse_ny_dt("2024-07-04 12:00:00")
    offset_min = dt.utcoffset().total_seconds() / 60
    assert offset_min == -240


def test_parse_ny_dt_handles_winter_offset():
    # January 2024 → EST (-05:00)
    dt = _parse_ny_dt("2024-01-15 09:30:00")
    offset_min = dt.utcoffset().total_seconds() / 60
    assert offset_min == -300
