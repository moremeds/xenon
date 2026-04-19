from datetime import datetime
from xenon.analysis.models import TickerData
from xenon.scanners.uw.context.pcr_sentiment import flag


def _td(pcr, earnings_within_14d=False):
    return TickerData(
        ticker="X", price=100.0, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=None, rv=None, iv_percentile=None, term_structure=None,
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=None, net_premium=None, pcr=pcr, darkpool=None,
        oi_changes=None, short_interest=None,
        earnings_date=None, earnings_within_14d=earnings_within_14d,
    )


def test_pcr_extreme_fear_flag():
    f = flag("X", _td(pcr=1.6))
    assert f is not None
    assert f.label == "Extreme Fear"


def test_pcr_complacent_flag():
    f = flag("X", _td(pcr=0.3))
    assert f is not None
    assert f.label == "Complacent"


def test_pcr_neutral_returns_none():
    assert flag("X", _td(pcr=1.0)) is None


def test_pcr_skipped_when_earnings_imminent():
    assert flag("X", _td(pcr=1.6, earnings_within_14d=True)) is None


def test_pcr_skipped_when_pcr_missing():
    assert flag("X", _td(pcr=None)) is None
