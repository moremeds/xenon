from datetime import datetime, date, timedelta
from scripts.analysis.models import TickerData
from scripts.uw_scan_lib.signals.earnings_iv_crush import detect


def _td(iv_pct, earnings_within_14d, earnings_date=None):
    return TickerData(
        ticker="X", price=100.0, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=None, rv=None, iv_percentile=iv_pct,
        term_structure=None, rr_skew_25d=None, vrp_history=None,
        flow_alerts=None, net_premium=None, pcr=None, darkpool=None,
        oi_changes=None, short_interest=None,
        earnings_date=earnings_date, earnings_within_14d=earnings_within_14d,
    )


def test_hit_on_high_iv_rank_and_earnings_in_window():
    td = _td(80.0, True, date.today() + timedelta(days=7))
    hit = detect("AAPL", td)
    assert hit is not None
    assert hit.tier == 1


def test_no_hit_when_iv_rank_too_low():
    td = _td(50.0, True, date.today() + timedelta(days=7))
    assert detect("AAPL", td) is None


def test_no_hit_when_earnings_not_in_window():
    assert detect("AAPL", _td(85.0, False)) is None


def test_no_hit_when_iv_rank_missing():
    td = _td(None, True, date.today() + timedelta(days=5))
    assert detect("AAPL", td) is None


def test_no_hit_when_earnings_date_unknown():
    assert detect("AAPL", _td(85.0, True, None)) is None
