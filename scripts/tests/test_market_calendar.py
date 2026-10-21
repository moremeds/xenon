"""Single US-equity market calendar (xenon.utils.market_calendar).

Regular session only: [09:30, 16:00) America/New_York, weekdays, minus the
holidays in src/xenon/config/market_holidays.json.
"""

import logging
import time as _time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from xenon.utils import market_calendar as mc

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh=12, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


# ── is_trading_day ────────────────────────────────────────────────────


def test_trading_day_plain_weekday():
    assert mc.is_trading_day(date(2026, 3, 3)) is True


def test_weekend_not_trading_day():
    assert mc.is_trading_day(date(2026, 3, 7)) is False
    assert mc.is_trading_day(date(2026, 3, 8)) is False


@pytest.mark.parametrize(
    "d",
    [
        date(2026, 1, 1),  # New Year
        date(2026, 1, 19),  # MLK
        date(2026, 4, 3),  # Good Friday
        date(2026, 7, 3),  # Independence Day observed
        date(2026, 11, 26),  # Thanksgiving 2026
        date(2026, 12, 25),
        date(2027, 1, 1),  # New Year 2027
        date(2027, 7, 5),  # Independence Day observed 2027
    ],
)
def test_configured_holidays_are_closed(d):
    assert mc.is_trading_day(d) is False


def test_day_after_thanksgiving_is_trading_day():
    assert mc.is_trading_day(date(2026, 11, 27)) is True


def test_unconfigured_year_raises_clear_error():
    with pytest.raises(mc.CalendarYearMissing, match="1999"):
        mc.is_trading_day(date(1999, 3, 3))


def test_weekend_in_unconfigured_year_still_raises():
    # A year we do not know is an error even when the weekday rule would answer.
    with pytest.raises(mc.CalendarYearMissing):
        mc.is_trading_day(date(1999, 3, 6))


def test_config_covers_current_year_and_next_from_october():
    today = date.today()
    mc.is_trading_day(date(today.year, 6, 1))
    if today.month >= 10:
        mc.is_trading_day(date(today.year + 1, 6, 1))


# ── is_open ───────────────────────────────────────────────────────────


def test_open_midday():
    assert mc.is_open(_et(2026, 3, 3, 10, 0)) is True


def test_open_boundaries_are_half_open():
    assert mc.is_open(_et(2026, 3, 3, 9, 29)) is False
    assert mc.is_open(_et(2026, 3, 3, 9, 30)) is True
    assert mc.is_open(_et(2026, 3, 3, 15, 59)) is True
    assert mc.is_open(_et(2026, 3, 3, 16, 0)) is False


def test_closed_weekend_and_holiday():
    assert mc.is_open(_et(2026, 3, 7, 12)) is False
    assert mc.is_open(_et(2026, 11, 26, 12)) is False  # Thanksgiving
    assert mc.is_open(_et(2027, 1, 1, 12)) is False  # New Year 2027


def test_naive_datetime_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        mc.is_open(datetime(2026, 3, 3, 10, 0))


def test_non_et_aware_datetime_is_converted():
    # 15:00 UTC on 2026-03-03 = 10:00 ET (EST)
    from datetime import timezone

    assert mc.is_open(datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)) is True
    # 21:30 UTC = 16:30 ET -> closed
    assert mc.is_open(datetime(2026, 3, 3, 21, 30, tzinfo=timezone.utc)) is False


def test_date_is_taken_in_et_not_utc():
    from datetime import timezone

    # 2026-11-27 02:00 UTC is still Thanksgiving evening in New York.
    assert mc.is_open(datetime(2026, 11, 27, 2, 0, tzinfo=timezone.utc)) is False
    # 2026-03-04 00:30 UTC is 19:30 ET on 03-03 -> closed (after hours)
    assert mc.is_open(datetime(2026, 3, 4, 0, 30, tzinfo=timezone.utc)) is False


def test_dst_edges():
    from datetime import timezone

    # US DST starts 2026-03-08 (Sun). Monday 03-09 is EDT: 09:30 EDT = 13:30 UTC.
    assert mc.is_open(datetime(2026, 3, 9, 13, 29, tzinfo=timezone.utc)) is False
    assert mc.is_open(datetime(2026, 3, 9, 13, 30, tzinfo=timezone.utc)) is True
    # Friday before the change is EST: 09:30 EST = 14:30 UTC.
    assert mc.is_open(datetime(2026, 3, 6, 14, 29, tzinfo=timezone.utc)) is False
    assert mc.is_open(datetime(2026, 3, 6, 14, 30, tzinfo=timezone.utc)) is True
    # DST ends 2026-11-01 (Sun). Monday 11-02 is EST: 16:00 EST = 21:00 UTC.
    assert mc.is_open(datetime(2026, 11, 2, 20, 59, tzinfo=timezone.utc)) is True
    assert mc.is_open(datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)) is False


def test_is_open_unconfigured_year_strict_raises():
    with pytest.raises(mc.CalendarYearMissing):
        mc.is_open(_et(1999, 3, 3, 10))


def test_is_open_lenient_falls_back_to_weekday_rule_and_logs(caplog):
    with caplog.at_level(logging.ERROR, logger=mc.logger.name):
        assert mc.is_open(_et(1999, 3, 3, 10), strict=False) is True
        assert mc.is_open(_et(1999, 3, 3, 17), strict=False) is False
        assert mc.is_open(_et(1999, 3, 6, 10), strict=False) is False  # Saturday
    assert any("1999" in r.getMessage() for r in caplog.records)


# ── host timezone independence ────────────────────────────────────────


@pytest.fixture
def hk_tz(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Hong_Kong")
    _time.tzset()
    yield
    monkeypatch.undo()
    _time.tzset()


def test_host_timezone_does_not_change_answers(hk_tz):
    assert datetime.now().astimezone().utcoffset() == timedelta(hours=8)
    assert mc.is_open(_et(2026, 3, 3, 10, 0)) is True
    assert mc.is_open(_et(2026, 3, 3, 17, 0)) is False
    assert mc.is_open(_et(2026, 11, 26, 12)) is False


def test_now_et_and_session_date_ignore_host_timezone(hk_tz):
    et_now = mc.now_et()
    assert et_now.tzinfo is not None
    assert et_now.utcoffset() in (timedelta(hours=-5), timedelta(hours=-4))
    assert mc.current_session_date_et() == et_now.date() or abs(
        (mc.current_session_date_et() - et_now.date()).days
    ) <= 1


# ── last_close ────────────────────────────────────────────────────────


def test_last_close_after_close_is_today():
    assert mc.last_close(_et(2026, 3, 3, 17, 0)) == _et(2026, 3, 3, 16, 0)
    assert mc.last_close(_et(2026, 3, 3, 16, 0)) == _et(2026, 3, 3, 16, 0)


def test_last_close_during_session_is_previous_trading_day():
    assert mc.last_close(_et(2026, 3, 3, 10, 0)) == _et(2026, 3, 2, 16, 0)


def test_last_close_before_open_monday_is_friday():
    assert mc.last_close(_et(2026, 3, 2, 8, 0)) == _et(2026, 2, 27, 16, 0)


def test_last_close_weekend_is_friday():
    assert mc.last_close(_et(2026, 3, 8, 12, 0)) == _et(2026, 3, 6, 16, 0)


def test_last_close_skips_holiday():
    # Fri 2026-11-27 morning: Thanksgiving closed, last close Wed 11-25.
    assert mc.last_close(_et(2026, 11, 27, 8, 0)) == _et(2026, 11, 25, 16, 0)
    # Thanksgiving itself
    assert mc.last_close(_et(2026, 11, 26, 18, 0)) == _et(2026, 11, 25, 16, 0)


def test_last_close_naive_rejected():
    with pytest.raises(ValueError):
        mc.last_close(datetime(2026, 3, 3, 17, 0))


# ── session date ──────────────────────────────────────────────────────


def test_current_session_date_et_is_date():
    assert isinstance(mc.current_session_date_et(), date)


def test_unreadable_holiday_file_is_calendar_error_not_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(mc, "_CONFIG_PATH", tmp_path / "missing.json")
    mc._load_file.cache_clear()
    mc._holidays.cache_clear()
    try:
        with pytest.raises(mc.CalendarYearMissing, match="cannot read"):
            mc.is_trading_day(date(2026, 3, 3))
        assert mc.is_open(_et(2026, 3, 3, 10), strict=False) is True
    finally:
        monkeypatch.undo()
        mc._load_file.cache_clear()
        mc._holidays.cache_clear()


def test_cli_main_prints_status(capsys):
    mc.main([])
    out = capsys.readouterr().out
    assert "Market status:" in out and "Last market close:" in out
