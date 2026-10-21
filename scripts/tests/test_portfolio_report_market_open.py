"""portfolio_report keeps its open fallback but logs a calendar failure at ERROR."""

from xenon.reports import portfolio_report
from xenon.utils import market_calendar


def test_calendar_failure_assumes_open_and_logs_error(monkeypatch, caplog):
    def boom(_now):
        raise market_calendar.CalendarYearMissing("no entry for 2099")

    monkeypatch.setattr(market_calendar, "is_open", boom)
    with caplog.at_level("ERROR"):
        assert portfolio_report._detect_market_open() is True
    rec = [r for r in caplog.records if r.levelname == "ERROR"]
    assert rec and "2099" in caplog.text and "assumes the market is open" in rec[0].getMessage()


def test_calendar_ok_returns_calendar_answer(monkeypatch, caplog):
    monkeypatch.setattr(market_calendar, "is_open", lambda _now: False)
    with caplog.at_level("ERROR"):
        assert portfolio_report._detect_market_open() is False
    assert not caplog.records
