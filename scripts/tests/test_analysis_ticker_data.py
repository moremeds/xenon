from datetime import datetime
from unittest.mock import MagicMock

from scripts.analysis.ticker_data import fetch_ticker_data


def _make_mock_client(*, vol_stats=None, gex=None, gex_by_strike=None,
                      term_structure=None, flow_alerts=None, oi_changes=None,
                      darkpool=None, earnings=None, short_data=None):
    c = MagicMock()
    c.get_volatility_stats.return_value = vol_stats or {}
    c.get_greek_exposure.return_value = gex or {}
    c.get_greek_exposure_by_strike.return_value = gex_by_strike or {}
    c.get_volatility_term_structure.return_value = term_structure or {}
    c.get_flow_alerts.return_value = {"data": flow_alerts or []}
    c.get_darkpool_flow.return_value = darkpool or {}
    c.get_earnings_by_ticker.return_value = earnings or {"data": []}
    c.get_short_data.return_value = short_data or {}
    return c


def test_iv_rank_normalization_raw_float_to_percentile():
    """Raw UW iv_rank=0.65 must become iv_percentile=65.0 (0-100 scale)."""
    c = _make_mock_client(vol_stats={"iv": "0.28", "rv": "0.21", "iv_rank": "0.65"})
    td = fetch_ticker_data("TSLA", c)
    assert td.iv_percentile == 65.0
    assert td.iv == 28.0   # also scaled to 0-100
    assert td.rv == 21.0


def test_missing_vol_stats_leaves_fields_none():
    c = _make_mock_client(vol_stats={})
    td = fetch_ticker_data("TSLA", c)
    assert td.iv is None
    assert td.rv is None
    assert td.iv_percentile is None


def test_earnings_within_14d_true_when_unknown():
    c = _make_mock_client(earnings={"data": []})
    td = fetch_ticker_data("TSLA", c)
    assert td.earnings_within_14d is True  # conservative default


def test_ticker_data_ticker_is_uppercased():
    c = _make_mock_client()
    td = fetch_ticker_data("tsla", c)
    assert td.ticker == "TSLA"


def test_ticker_data_fetched_at_is_datetime():
    c = _make_mock_client()
    td = fetch_ticker_data("TSLA", c)
    assert isinstance(td.fetched_at, datetime)


def test_vrp_history_populated_when_wrapper_available():
    c = _make_mock_client()
    c.get_variance_risk_premium = MagicMock(return_value={
        "data": [{"vrp": 0.1}, {"vrp": 0.2}, {"vrp": 0.3}],
    })
    td = fetch_ticker_data("TSLA", c)
    assert td.vrp_history == [0.1, 0.2, 0.3]


def test_vrp_history_none_when_wrapper_missing():
    c = _make_mock_client()
    # No get_variance_risk_premium attribute at all
    if hasattr(c, "get_variance_risk_premium"):
        del c.get_variance_risk_premium
    td = fetch_ticker_data("TSLA", c)
    assert td.vrp_history is None


def test_pcr_derived_from_flow_alerts_call_put_counts():
    c = _make_mock_client(flow_alerts=[
        {"option_type": "call"}, {"option_type": "call"}, {"option_type": "put"},
    ])
    td = fetch_ticker_data("TSLA", c)
    assert td.pcr == 0.5  # 1 put / 2 calls


def test_pcr_none_when_no_calls():
    c = _make_mock_client(flow_alerts=[{"option_type": "put"}])
    td = fetch_ticker_data("TSLA", c)
    assert td.pcr is None
