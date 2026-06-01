"""POST /futu/history/sync rejects bad `since` values with structured errors."""

from datetime import date

import pytest

from xenon.api.services.futu_history import (
    MIN_HISTORICAL_DATE,
    SinceOutOfRange,
    SinceUnparseable,
    parse_since,
)


def test_auto_returns_none():
    assert parse_since("auto") is None


def test_none_returns_none():
    assert parse_since(None) is None


def test_iso_date_in_range():
    assert parse_since("2024-06-15") == date(2024, 6, 15)


def test_before_floor_rejected():
    with pytest.raises(SinceOutOfRange) as exc:
        parse_since("2020-01-01")
    assert exc.value.reason_code == "since_out_of_range"


def test_future_date_rejected():
    with pytest.raises(SinceOutOfRange):
        parse_since("2099-01-01")


def test_garbage_rejected():
    with pytest.raises(SinceUnparseable):
        parse_since("yesterday")


def test_floor_constant_is_may_first_2024():
    assert MIN_HISTORICAL_DATE == date(2024, 5, 1)
