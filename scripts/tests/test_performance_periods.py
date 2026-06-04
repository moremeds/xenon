"""Period resolver — pure function, no DB."""

from __future__ import annotations

from datetime import date

import pytest

from xenon.api.services.performance_periods import (
    SUPPORTED_PERIODS,
    InvalidPeriodError,
    resolve_period_start,
)


def test_ytd_returns_jan_1_of_as_of_year():
    assert resolve_period_start("YTD", as_of=date(2026, 6, 3), inception=date(2021, 1, 18)) == date(2026, 1, 1)


def test_one_month_returns_30_days_back():
    assert resolve_period_start("1M", as_of=date(2026, 6, 3), inception=date(2021, 1, 18)) == date(2026, 5, 4)


def test_three_month_returns_90_days_back():
    assert resolve_period_start("3M", as_of=date(2026, 6, 3), inception=date(2021, 1, 18)) == date(2026, 3, 5)


def test_all_returns_inception_when_inception_provided():
    assert resolve_period_start("All", as_of=date(2026, 6, 3), inception=date(2021, 1, 18)) == date(2021, 1, 18)


def test_all_falls_back_to_ytd_when_inception_missing():
    assert resolve_period_start("All", as_of=date(2026, 6, 3), inception=None) == date(2026, 1, 1)


def test_period_clamped_to_inception():
    """1M back from 2021-02-01 with inception 2021-01-18 → use inception."""
    assert resolve_period_start("1M", as_of=date(2021, 2, 1), inception=date(2021, 1, 18)) == date(2021, 1, 18)


def test_invalid_period_raises():
    with pytest.raises(InvalidPeriodError):
        resolve_period_start("6M", as_of=date(2026, 6, 3), inception=None)


def test_supported_periods_constant():
    assert SUPPORTED_PERIODS == ("1M", "3M", "YTD", "All")


def test_case_normalization_ytd_lower():
    assert resolve_period_start("ytd", as_of=date(2026, 6, 3), inception=None) == date(2026, 1, 1)


def test_case_normalization_all_lower():
    assert resolve_period_start("all", as_of=date(2026, 6, 3), inception=date(2024, 1, 1)) == date(2024, 1, 1)


def test_ytd_does_not_clamp_when_inception_is_earlier():
    """When inception is well before Jan 1 of current year, YTD starts Jan 1."""
    assert resolve_period_start("YTD", as_of=date(2026, 6, 3), inception=date(2020, 1, 1)) == date(2026, 1, 1)
