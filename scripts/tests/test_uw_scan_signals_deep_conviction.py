from datetime import datetime
from xenon.analysis.models import TickerData
from xenon.scanners.uw.signals.deep_conviction_flow import detect


def _td(flow_alerts=None, earnings_within_14d=False):
    return TickerData(
        ticker="TSLA", price=200.0, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=None, rv=None, iv_percentile=None, term_structure=None,
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=flow_alerts, net_premium=None, pcr=None, darkpool=None,
        oi_changes=None, short_interest=None,
        earnings_date=None, earnings_within_14d=earnings_within_14d,
    )


_BASE = {
    "volume": 5000, "open_interest": 1000, "ask_side_percent": "0.85",
    "total_premium": 2_000_000, "multileg_percent": "0.05",
    "moneyness": "0.03", "expiry_dte": 21,
}


def test_deep_conviction_hit_on_aggressive_ask_side_single_leg():
    hit = detect("TSLA", _td(flow_alerts=[dict(_BASE)]))
    assert hit is not None
    assert hit.tier == 1
    assert hit.signal_type == "deep_conviction_flow"


def test_no_hit_when_premium_too_low():
    a = dict(_BASE, total_premium=100_000)
    assert detect("TSLA", _td(flow_alerts=[a])) is None


def test_no_hit_when_volume_not_exceeding_oi():
    a = dict(_BASE, volume=500)
    assert detect("TSLA", _td(flow_alerts=[a])) is None


def test_no_hit_when_bid_side_dominant():
    a = dict(_BASE, ask_side_percent="0.40")
    assert detect("TSLA", _td(flow_alerts=[a])) is None


def test_no_hit_when_earnings_within_window():
    assert detect("TSLA", _td(flow_alerts=[dict(_BASE)], earnings_within_14d=True)) is None


def test_no_hit_when_no_flow_alerts():
    assert detect("TSLA", _td(flow_alerts=None)) is None
    assert detect("TSLA", _td(flow_alerts=[])) is None
