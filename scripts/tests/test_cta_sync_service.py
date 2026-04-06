"""Tests for CTA sync scheduling and catch-up behavior."""

from datetime import date, datetime

from utils.cta_sync import CTA_SYNC_ET_SLOTS, latest_closed_trading_day
from utils.launchd_schedule import convert_et_calendar_entries, expand_intraday_slots


class TestLatestClosedTradingDay:
    def test_after_close_uses_same_trading_day(self):
        now = datetime(2026, 3, 12, 16, 5)
        assert latest_closed_trading_day(now) == "2026-03-12"

    def test_before_close_uses_previous_trading_day(self):
        now = datetime(2026, 3, 12, 15, 59)
        assert latest_closed_trading_day(now) == "2026-03-11"

    def test_weekend_uses_prior_friday(self):
        now = datetime(2026, 3, 14, 12, 0)
        assert latest_closed_trading_day(now) == "2026-03-13"


class TestLaunchdCalendarEntries:
    def test_data_refresh_slots_convert_from_et_to_pacific(self):
        slots = expand_intraday_slots((9, 30), (10, 0), 15)
        entries = convert_et_calendar_entries(
            [(1, hour, minute) for hour, minute in slots],
            local_tz="America/Los_Angeles",
            reference_date=date(2026, 3, 9),
        )

        assert entries == [
            {"Weekday": 1, "Hour": 6, "Minute": 30},
            {"Weekday": 1, "Hour": 6, "Minute": 45},
            {"Weekday": 1, "Hour": 7, "Minute": 0},
        ]

    def test_cta_slots_only_include_two_post_close_runs(self):
        entries = convert_et_calendar_entries(
            [(1, hour, minute) for hour, minute in CTA_SYNC_ET_SLOTS],
            local_tz="America/Los_Angeles",
            reference_date=date(2026, 3, 9),
        )

        assert entries == [
            {"Weekday": 1, "Hour": 13, "Minute": 15},
            {"Weekday": 1, "Hour": 14, "Minute": 0},
        ]
