from datetime import datetime
from zoneinfo import ZoneInfo

from xenon.execution.market_hours import is_opt_tradeable, is_rth

NYC = ZoneInfo("America/New_York")


def _at(year, month, day, hour, minute) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NYC)


def test_rth_open_at_930_weekday():
    assert is_rth(_at(2026, 4, 22, 9, 30)) is True


def test_rth_closed_at_1600_weekday():
    assert is_rth(_at(2026, 4, 22, 16, 0)) is False


def test_rth_closed_on_saturday():
    assert is_rth(_at(2026, 4, 25, 12, 0)) is False


def test_opt_not_tradeable_premarket():
    assert is_opt_tradeable(_at(2026, 4, 22, 9, 0)) is False


def test_opt_tradeable_midday():
    assert is_opt_tradeable(_at(2026, 4, 22, 13, 0)) is True
