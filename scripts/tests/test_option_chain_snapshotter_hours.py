"""TDD: RTH gating for the option-chain snapshotter.

The snapshotter must skip cleanly outside NYSE regular trading hours
(weekends, holidays, before 09:30 ET, after 16:00 ET) so launchd can fire
every 10 minutes 24/7 without polluting `archive.snapshot_run` with
guaranteed-failing runs.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from xenon.option_chain_snapshotter.hours import is_nyse_rth


@pytest.mark.parametrize(
    "iso_utc, expected, label",
    [
        # 2026-06-04 was a Thursday. RTH = 09:30–16:00 ET = 13:30–20:00 UTC (EDT).
        ("2026-06-04T13:25:00+00:00", False, "1 min before open"),
        ("2026-06-04T13:30:00+00:00", True, "at open"),
        ("2026-06-04T14:00:00+00:00", True, "mid-session"),
        ("2026-06-04T19:59:00+00:00", True, "1 min before close"),
        ("2026-06-04T20:00:00+00:00", False, "at close (16:00 ET is half-open)"),
        ("2026-06-04T20:30:00+00:00", False, "after close"),
        # 2026-06-06 is a Saturday — weekend.
        ("2026-06-06T15:00:00+00:00", False, "Saturday mid-day"),
        # 2026-07-03 — observance of Independence Day (July 4 falls on Saturday
        # in 2026, so NYSE observes the holiday on Friday). Fully closed all day.
        ("2026-07-03T16:00:00+00:00", False, "July 3 (July 4 observed, closed)"),
        # 2026-11-27 — day after Thanksgiving (Black Friday), early-close at 13:00 ET (18:00 UTC).
        ("2026-11-27T15:00:00+00:00", True, "Black Friday 10:00 ET — inside early session"),
        ("2026-11-27T18:30:00+00:00", False, "Black Friday 13:30 ET — after early close"),
        # 2026-12-25 — Christmas, NYSE closed.
        ("2026-12-25T15:00:00+00:00", False, "Christmas day closed"),
    ],
)
def test_is_nyse_rth_calendar_boundaries(iso_utc: str, expected: bool, label: str) -> None:
    ts = datetime.fromisoformat(iso_utc).astimezone(timezone.utc)
    assert is_nyse_rth(ts) is expected, f"{label}: expected {expected} at {iso_utc}"


def test_is_nyse_rth_naive_datetime_rejected() -> None:
    """A naive datetime is ambiguous — refuse it explicitly to avoid silent
    bugs when callers forget timezone awareness in tests or scripts."""
    naive = datetime(2026, 6, 4, 14, 0, 0)
    with pytest.raises(ValueError, match="tz-aware"):
        is_nyse_rth(naive)
