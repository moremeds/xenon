"""Tests for current_session_date_et helper (correction #1)."""
from datetime import date, datetime
import pytz
from xenon.utils.market_calendar import current_session_date_et


def test_returns_date_object():
    assert isinstance(current_session_date_et(), date)


def test_matches_et_now_date():
    et = pytz.timezone("America/New_York")
    expected = datetime.now(et).date()
    actual = current_session_date_et()
    # Within 1 day to handle the tiny race across midnight ET.
    assert abs((actual - expected).days) <= 1
