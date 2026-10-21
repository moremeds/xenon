"""The snapshot-staleness threshold follows the shared market calendar.

On a market holiday the CLOSED threshold (1800 s) applies, not the RTH one (300 s).
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from xenon.api.services import order_entry
from xenon.execution import gate4
from xenon.utils import market_calendar

ET = ZoneInfo("America/New_York")
THANKSGIVING_NOON = datetime(2026, 11, 26, 12, 0, tzinfo=ET)
WEDNESDAY_NOON = datetime(2026, 11, 25, 12, 0, tzinfo=ET)


@pytest.fixture(autouse=True)
def _default_thresholds(monkeypatch):
    monkeypatch.delenv("XENON_PORTFOLIO_SNAPSHOT_STALE_S", raising=False)
    monkeypatch.delenv("XENON_PORTFOLIO_SNAPSHOT_STALE_CLOSED_S", raising=False)


def _twenty_min_old() -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=20)


def test_gate4_holiday_uses_closed_threshold(monkeypatch):
    monkeypatch.setattr(market_calendar, "now_et", lambda: THANKSGIVING_NOON)
    assert gate4._snapshot_stale(_twenty_min_old()) is False


def test_gate4_trading_day_uses_rth_threshold(monkeypatch):
    monkeypatch.setattr(market_calendar, "now_et", lambda: WEDNESDAY_NOON)
    assert gate4._snapshot_stale(_twenty_min_old()) is True


def test_order_entry_holiday_uses_closed_threshold(monkeypatch):
    monkeypatch.setattr(market_calendar, "now_et", lambda: THANKSGIVING_NOON)
    assert order_entry._portfolio_snapshot_stale_response(_twenty_min_old()) is None


def test_order_entry_trading_day_uses_rth_threshold(monkeypatch):
    monkeypatch.setattr(market_calendar, "now_et", lambda: WEDNESDAY_NOON)
    verdict = order_entry._portfolio_snapshot_stale_response(_twenty_min_old())
    assert verdict is not None and verdict.accept is False


# 20-minute-old snapshot: stale under the market-open limit (300 s), fresh under the closed one (1800 s).
# The whole 16:00 minute keeps the open limit (old behaviour); 16:01:00 is closed.
_BOUNDARY = [
    ((15, 59, 59), True),
    ((16, 0, 0), True),
    ((16, 0, 59), True),
    ((16, 1, 0), False),
]


@pytest.mark.parametrize("hms,stale", _BOUNDARY)
def test_gate4_closing_minute_boundary(monkeypatch, hms, stale):
    at = datetime(2026, 11, 25, *hms, tzinfo=ET)
    monkeypatch.setattr(market_calendar, "now_et", lambda: at)
    assert gate4._snapshot_stale(_twenty_min_old()) is stale


@pytest.mark.parametrize("hms,stale", _BOUNDARY)
def test_order_entry_closing_minute_boundary(monkeypatch, hms, stale):
    at = datetime(2026, 11, 25, *hms, tzinfo=ET)
    monkeypatch.setattr(market_calendar, "now_et", lambda: at)
    verdict = order_entry._portfolio_snapshot_stale_response(_twenty_min_old())
    assert (verdict is not None) is stale
