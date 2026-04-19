from datetime import date, datetime
from scripts.analysis.models import TickerData
from scripts.scanners.uw.signals.gex_pinning import detect, MEGA_CAPS


def _td(ticker, gex_by_strike=None, price=100.0):
    return TickerData(
        ticker=ticker, price=price, fetched_at=datetime.now(),
        gex=None, gex_by_strike=gex_by_strike,
        iv=None, rv=None, iv_percentile=None, term_structure=None,
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=None, net_premium=None, pcr=None, darkpool=None,
        oi_changes=None, short_interest=None,
        earnings_date=None, earnings_within_14d=False,
    )


def test_mega_caps_contains_spy_and_qqq():
    assert "SPY" in MEGA_CAPS
    assert "QQQ" in MEGA_CAPS


def test_no_hit_outside_mega_caps():
    td = _td("WULF", gex_by_strike={"strikes": [{"strike": 100, "gamma": 5.0}]})
    assert detect("WULF", td, today=date(2026, 4, 17)) is None


def test_no_hit_outside_opex_week():
    td = _td("SPY", gex_by_strike={"strikes": [{"strike": 450, "gamma": 10.0}]}, price=450.5)
    assert detect("SPY", td, today=date(2026, 4, 1)) is None


def test_hit_in_opex_week_with_near_wall():
    td = _td("SPY", gex_by_strike={"strikes": [
        {"strike": 450, "gamma": 10.0},
        {"strike": 455, "gamma": 0.1},
    ]}, price=450.5)
    hit = detect("SPY", td, today=date(2026, 4, 17))
    assert hit is not None
    assert hit.signal_type == "gex_pinning"


def test_no_hit_when_gex_by_strike_missing():
    td = _td("SPY", gex_by_strike=None)
    assert detect("SPY", td, today=date(2026, 4, 17)) is None
