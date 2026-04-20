from datetime import datetime
from xenon.analysis.models import TickerData
from xenon.scanners.uw.signals.dark_pool_accumulation import detect


def _td(darkpool):
    return TickerData(
        ticker="X", price=100.0, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=None, rv=None, iv_percentile=None, term_structure=None,
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=None, net_premium=None, pcr=None, darkpool=darkpool,
        oi_changes=None, short_interest=None,
        earnings_date=None, earnings_within_14d=False,
    )


def test_hit_on_three_large_prints_at_similar_level():
    dp = {"data": [
        {"price": 100.0, "premium": 1_500_000},
        {"price": 100.3, "premium": 2_000_000},
        {"price": 100.2, "premium": 1_200_000},
    ]}
    hit = detect("TSLA", _td(dp))
    assert hit is not None
    assert hit.tier == 2
    assert hit.evidence["direction_neutral"] is True


def test_no_hit_with_only_two_large_prints():
    dp = {"data": [
        {"price": 100.0, "premium": 1_500_000},
        {"price": 100.3, "premium": 2_000_000},
    ]}
    assert detect("TSLA", _td(dp)) is None


def test_no_hit_with_spread_out_prices():
    dp = {"data": [
        {"price": 100.0, "premium": 1_500_000},
        {"price": 105.0, "premium": 2_000_000},
        {"price": 110.0, "premium": 1_200_000},
    ]}
    assert detect("TSLA", _td(dp)) is None


def test_no_hit_when_darkpool_missing():
    assert detect("TSLA", _td(None)) is None
