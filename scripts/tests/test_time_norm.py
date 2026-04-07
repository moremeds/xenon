"""Tests for scripts/utils/time_norm.py.

Covers the tribunal-flagged correctness hazards:
- naive datetimes are rejected
- HKT → UTC conversion is exact
- DST transitions do not shift by an hour
- round-trip `iso_z(parse_iso_z(x)) == x` (ms precision)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from zoneinfo import ZoneInfo

from scripts.utils.time_norm import (
    from_futu_naive,
    from_futu_us_eastern,
    iso_z,
    now_utc,
    parse_iso_z,
)

HKT = ZoneInfo("Asia/Hong_Kong")
US_EAST = ZoneInfo("America/New_York")


# ────────────────────────────────────────────────────────────────────
# iso_z
# ────────────────────────────────────────────────────────────────────


def test_iso_z_rejects_naive_datetime():
    naive = datetime(2026, 4, 7, 12, 0, 0)
    with pytest.raises(ValueError, match="naive"):
        iso_z(naive)


def test_iso_z_formats_utc_with_ms_suffix_z():
    dt = datetime(2026, 4, 7, 12, 34, 56, 789000, tzinfo=timezone.utc)
    assert iso_z(dt) == "2026-04-07T12:34:56.789Z"


def test_iso_z_truncates_microseconds_to_ms():
    dt = datetime(2026, 4, 7, 12, 0, 0, 123456, tzinfo=timezone.utc)
    # 123456us → 123ms (truncated, not rounded)
    assert iso_z(dt) == "2026-04-07T12:00:00.123Z"


def test_iso_z_converts_hkt_to_utc():
    # 20:00 HKT == 12:00 UTC (HKT is UTC+8, no DST)
    dt = datetime(2026, 4, 7, 20, 0, 0, tzinfo=HKT)
    assert iso_z(dt) == "2026-04-07T12:00:00.000Z"


# ────────────────────────────────────────────────────────────────────
# parse_iso_z
# ────────────────────────────────────────────────────────────────────


def test_parse_iso_z_accepts_z_suffix():
    dt = parse_iso_z("2026-04-07T12:34:56.789Z")
    assert dt == datetime(2026, 4, 7, 12, 34, 56, 789000, tzinfo=timezone.utc)


def test_parse_iso_z_accepts_plus_00_00():
    dt = parse_iso_z("2026-04-07T12:34:56.789+00:00")
    assert dt == datetime(2026, 4, 7, 12, 34, 56, 789000, tzinfo=timezone.utc)


def test_parse_iso_z_converts_other_offset_to_utc():
    dt = parse_iso_z("2026-04-07T20:34:56.789+08:00")  # HKT
    assert dt == datetime(2026, 4, 7, 12, 34, 56, 789000, tzinfo=timezone.utc)


def test_parse_iso_z_rejects_naive_string():
    with pytest.raises(ValueError, match="naive"):
        parse_iso_z("2026-04-07T12:34:56.789")


def test_parse_iso_z_rejects_empty():
    with pytest.raises(ValueError):
        parse_iso_z("")


def test_parse_iso_z_rejects_garbage():
    with pytest.raises(ValueError):
        parse_iso_z("not a date")


# ────────────────────────────────────────────────────────────────────
# round-trip identity
# ────────────────────────────────────────────────────────────────────


def test_round_trip_ms_precision():
    original = "2026-04-07T12:34:56.789Z"
    assert iso_z(parse_iso_z(original)) == original


def test_round_trip_zero_ms():
    original = "2026-04-07T12:34:56.000Z"
    assert iso_z(parse_iso_z(original)) == original


def test_round_trip_across_dst_spring_forward_hkt():
    # HKT has no DST, so this should be a no-op at the UTC layer —
    # specifically tests that the round-trip doesn't drift when the
    # US source tz would have shifted.
    dt = datetime(2026, 3, 8, 2, 30, 0, tzinfo=HKT)  # US spring-forward date
    s = iso_z(dt)
    assert parse_iso_z(s) == dt.astimezone(timezone.utc)


# ────────────────────────────────────────────────────────────────────
# from_futu_naive / from_futu_us_eastern
# ────────────────────────────────────────────────────────────────────


def test_from_futu_naive_assumes_hkt_by_default():
    dt = from_futu_naive("2026-04-07 20:00:00")  # space-separated, Futu style
    assert dt == datetime(2026, 4, 7, 12, 0, 0, tzinfo=timezone.utc)


def test_from_futu_naive_passes_through_aware_datetime():
    aware = datetime(2026, 4, 7, 12, 0, 0, tzinfo=timezone.utc)
    assert from_futu_naive(aware) == aware


def test_from_futu_us_eastern_winter_est():
    # 2026-01-15 10:00 EST = 15:00 UTC (UTC-5)
    dt = from_futu_us_eastern("2026-01-15 10:00:00")
    assert dt == datetime(2026, 1, 15, 15, 0, 0, tzinfo=timezone.utc)


def test_from_futu_us_eastern_summer_edt():
    # 2026-07-15 10:00 EDT = 14:00 UTC (UTC-4) — DST active
    dt = from_futu_us_eastern("2026-07-15 10:00:00")
    assert dt == datetime(2026, 7, 15, 14, 0, 0, tzinfo=timezone.utc)


def test_from_futu_us_eastern_dst_spring_forward_edge():
    # 2026-03-08 is when US clocks jump 02:00 → 03:00 EST→EDT.
    # 03:30 local is unambiguous EDT.
    dt = from_futu_us_eastern("2026-03-08 03:30:00")
    # 03:30 EDT = 07:30 UTC
    assert dt == datetime(2026, 3, 8, 7, 30, 0, tzinfo=timezone.utc)


# ────────────────────────────────────────────────────────────────────
# now_utc
# ────────────────────────────────────────────────────────────────────


def test_now_utc_is_aware_and_utc():
    dt = now_utc()
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0
