"""DST-correctness tests for MonitorDaemon.is_market_hours().

Spec §10.4.1 (codex N-S5): the existing UTC-5 arithmetic is wrong during EDT.
This test pins the expected behavior across both timezones, weekdays, and the
standard 9:30-16:00 ET window.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from xenon.monitor_daemon.daemon import MonitorDaemon


def _at_utc(year, month, day, hour, minute):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


@pytest.fixture
def daemon():
    return MonitorDaemon(state_file=None, respect_market_hours=True)


@pytest.mark.parametrize(
    "utc_dt, expected, description",
    [
        # EST (winter, UTC-5)
        (_at_utc(2026, 1, 12, 14, 35), True, "Mon 9:35 ET in EST -> open"),
        (_at_utc(2026, 1, 12, 13, 35), False, "Mon 8:35 ET in EST -> closed"),
        (_at_utc(2026, 1, 12, 21, 5), False, "Mon 16:05 ET in EST -> closed"),
        # EDT (summer, UTC-4)
        (_at_utc(2026, 7, 13, 13, 35), True, "Mon 9:35 ET in EDT -> open"),
        (_at_utc(2026, 7, 13, 12, 35), False, "Mon 8:35 ET in EDT -> closed"),
        (_at_utc(2026, 7, 13, 20, 5), False, "Mon 16:05 ET in EDT -> closed"),
        # DST transition: spring forward (2026-03-08 02:00 EST -> 03:00 EDT)
        (
            _at_utc(2026, 3, 9, 14, 35),
            True,
            "Mon 10:35 ET first weekday after spring-forward (EDT now); UTC 14:35 = 10:35 EDT -> open",
        ),
        (
            _at_utc(2026, 3, 9, 13, 35),
            True,
            "Mon 9:35 ET first weekday after spring-forward; UTC 13:35 = 9:35 EDT -> open",
        ),
        # DST transition: fall back (2026-11-01 02:00 EDT -> 01:00 EST)
        (
            _at_utc(2026, 11, 2, 14, 35),
            True,
            "Mon 9:35 ET first weekday after fall-back (EST now); UTC 14:35 = 9:35 EST -> open",
        ),
        # Weekends always closed
        (_at_utc(2026, 1, 10, 14, 35), False, "Sat 9:35 ET -> closed"),
        (_at_utc(2026, 1, 11, 14, 35), False, "Sun 9:35 ET -> closed"),
    ],
)
def test_is_market_hours_dst_correctness(daemon, utc_dt, expected, description):
    with patch("xenon.monitor_daemon.daemon.datetime") as mock_dt:
        mock_dt.now.return_value = utc_dt
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert daemon.is_market_hours() is expected, description


def test_is_market_hours_uses_new_york_timezone(daemon):
    """Smoke check that the helper does not silently fall back to UTC-5 arithmetic.

    By feeding 13:35 UTC on a winter day (=8:35 EST -> closed) and 13:35 UTC on
    a summer day (=9:35 EDT -> open), we prove the implementation actually
    walked through zoneinfo, not a static offset.
    """
    winter_morning = _at_utc(2026, 1, 12, 13, 35)
    summer_morning = _at_utc(2026, 7, 13, 13, 35)

    with patch("xenon.monitor_daemon.daemon.datetime") as mock_dt:
        mock_dt.now.return_value = winter_morning
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert daemon.is_market_hours() is False

    with patch("xenon.monitor_daemon.daemon.datetime") as mock_dt:
        mock_dt.now.return_value = summer_morning
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert daemon.is_market_hours() is True
