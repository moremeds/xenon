"""NYSE trading-calendar helper tests.

Task 2 of docs/superpowers/plans/2026-06-01-futu-account-performance-reconstruction.md.
"""

from datetime import date

from xenon.utils.market_calendar import (
    is_trading_day,
    previous_trading_day,
    trading_days_in_range,
)


def test_is_trading_day_weekday():
    assert is_trading_day(date(2024, 5, 1)) is True  # Wed


def test_is_trading_day_weekend():
    assert is_trading_day(date(2024, 5, 4)) is False  # Sat


def test_is_trading_day_us_holiday():
    assert is_trading_day(date(2024, 7, 4)) is False  # Independence Day


def test_previous_trading_day_skips_weekend():
    # Monday 2024-05-06 → previous trading day is Friday 2024-05-03
    assert previous_trading_day(date(2024, 5, 6)) == date(2024, 5, 3)


def test_previous_trading_day_skips_holiday():
    # Day after July 4 (a Thursday holiday) → Wed July 3.
    assert previous_trading_day(date(2024, 7, 5)) == date(2024, 7, 3)


def test_trading_days_in_range_inclusive():
    days = trading_days_in_range(date(2024, 7, 1), date(2024, 7, 5))
    # July 1 Mon, July 2 Tue, July 3 Wed, July 5 Fri (July 4 is the holiday)
    assert days == [
        date(2024, 7, 1),
        date(2024, 7, 2),
        date(2024, 7, 3),
        date(2024, 7, 5),
    ]
