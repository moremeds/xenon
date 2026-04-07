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


# ════════════════════════════════════════════════════════════════════
# Deep mode (commit 3) — payload normalization for 6 enrichment endpoints
# ════════════════════════════════════════════════════════════════════


def _deep_mock_client():
    c = MagicMock()
    c.get_volatility_stats.return_value = {}
    c.get_volatility_term_structure.return_value = {}
    c.get_greek_exposure.return_value = {}
    # Real UW shape: {"data": [{date, strike, call_gex, put_gex, ...}]}
    c.get_greek_exposure_by_strike.return_value = {
        "data": [
            {"date": "2026-04-07", "strike": "99", "call_gex": "0.5", "put_gex": "-2.0"},
            {"date": "2026-04-07", "strike": "100", "call_gex": "2.5", "put_gex": "-0.5"},
            {"date": "2026-04-07", "strike": "101", "call_gex": "1.5", "put_gex": "-0.5"},
        ]
    }
    c.get_flow_alerts.return_value = {"data": []}
    c.get_darkpool_flow.return_value = {}
    c.get_earnings_by_ticker.return_value = {"data": []}
    c.get_short_data.return_value = {}
    c.get_historical_risk_reversal_skew.return_value = {"data": []}
    # 6 deep endpoints
    c.get_stock_state.return_value = {"data": {"close": "100.5"}}
    c.get_stock_info.return_value = {"data": {"sector": "Technology"}}
    c.get_options_volume.return_value = {
        "data": [{"net_call_premium": "5000.0", "net_put_premium": "-2000.0"}]
    }
    c.get_net_prem_ticks.return_value = {
        "data": [
            {"net_call_premium": "100", "net_put_premium": "-50"},
            {"net_call_premium": "200", "net_put_premium": "30"},
        ]
    }
    c.get_short_volume_ratio.return_value = {
        "si": [
            {"market_date": "2026-04-07", "short_volume_ratio": "0.55"},
            {"market_date": "2026-04-06", "short_volume_ratio": "0.50"},
            {"market_date": "2026-04-05", "short_volume_ratio": "0.48"},
            {"market_date": "2026-04-04", "short_volume_ratio": "0.45"},
        ]
    }
    c.get_iv_rank.return_value = {
        "data": [
            {"iv_rank_1y": "30.0", "volatility": "0.20"},
            {"iv_rank_1y": "45.0", "volatility": "0.25"},
            {"iv_rank_1y": "60.0", "volatility": "0.18"},
        ]
    }
    return c


def test_deep_false_does_not_call_enrichment_endpoints():
    """Scan-path perf regression — default mode must skip the 5 enrichment
    endpoints. NOTE: get_stock_state IS called on every fetch (deep or not)
    because td.price is foundational for downstream signals; that's the
    only required HTTP call beyond the legacy fetcher set."""
    c = _deep_mock_client()
    fetch_ticker_data("TSLA", c, deep=False)
    c.get_stock_info.assert_not_called()
    c.get_options_volume.assert_not_called()
    c.get_net_prem_ticks.assert_not_called()
    c.get_short_volume_ratio.assert_not_called()
    c.get_iv_rank.assert_not_called()


def test_deep_false_populates_price_from_stock_state():
    """C1 regression — scan path MUST populate td.price via get_stock_state.
    Without this, gex_pinning and wall scoring silently no-op on every scan
    ticker."""
    c = _deep_mock_client()
    td = fetch_ticker_data("TSLA", c, deep=False)
    assert td.price == 100.5


def test_deep_false_populates_gex_flip():
    """C2 regression — scan path MUST populate td.gex['flip'] from strikes.
    Without this, classify_regime can't compute gex_flip_relative and the
    market_structure score loses its flip-distance contribution."""
    c = _deep_mock_client()
    td = fetch_ticker_data("TSLA", c, deep=False)
    assert td.gex is not None
    assert td.gex.get("flip") is not None


def test_gex_envelope_never_empty_dict_when_both_endpoints_fail():
    """C3 regression — never create an empty {} envelope. The leftover stub
    code used to do `if gex is None: gex = {}` then never populate it,
    which made bucket_available('market_structure') return True with all-
    zero scores, silently consuming 28 weight points. Either gex has real
    signal (net or flip) or it stays None."""
    c = _deep_mock_client()
    c.get_greek_exposure.side_effect = RuntimeError("upstream 500")
    c.get_greek_exposure_by_strike.return_value = {}  # no strikes either
    td = fetch_ticker_data("TSLA", c, deep=False)
    assert td.gex is None


def test_gex_envelope_flip_only_when_only_strikes_succeed():
    """When get_greek_exposure raises but strikes succeed, the resulting
    gex envelope should contain at least the flip — flip alone is signal,
    and the market_structure scorer will partially contribute."""
    c = _deep_mock_client()
    c.get_greek_exposure.side_effect = RuntimeError("upstream 500")
    td = fetch_ticker_data("TSLA", c, deep=False)
    assert td.gex is not None
    assert td.gex.get("flip") is not None
    assert "net" not in td.gex  # no net signal because endpoint failed


def test_deep_true_populates_price_and_sector_from_stock_state_and_info():
    c = _deep_mock_client()
    td = fetch_ticker_data("TSLA", c, deep=True)
    assert td.price == 100.5
    assert td.sector == "Technology"


def test_deep_true_populates_options_volume_net_premiums():
    c = _deep_mock_client()
    td = fetch_ticker_data("TSLA", c, deep=True)
    assert td.net_call_premium == 5000.0
    assert td.net_put_premium == -2000.0


def test_deep_true_aggregates_net_prem_ticks_into_dict():
    c = _deep_mock_client()
    td = fetch_ticker_data("TSLA", c, deep=True)
    assert td.net_premium is not None
    assert td.net_premium["net_call_premium"] == 300.0  # 100 + 200
    assert td.net_premium["net_put_premium"] == -20.0   # -50 + 30
    assert td.net_premium["tick_count"] == 2


def test_deep_true_short_volume_ratio_and_3day_trend():
    c = _deep_mock_client()
    td = fetch_ticker_data("TSLA", c, deep=True)
    assert td.short_volume_ratio == 0.55  # newest by market_date
    assert td.short_volume_trend == [0.55, 0.50, 0.48]  # last 3, newest first


def test_deep_true_iv_rank_and_52w_iv_range():
    c = _deep_mock_client()
    td = fetch_ticker_data("TSLA", c, deep=True)
    assert td.iv_rank == 60.0   # latest entry's iv_rank_1y
    assert td.iv_52w_low == 18.0  # min(0.18,0.20,0.25) * 100
    assert td.iv_52w_high == 25.0


def test_deep_true_extracts_walls_and_gamma_per_1pct_from_strikes():
    c = _deep_mock_client()
    td = fetch_ticker_data("TSLA", c, deep=True)
    # call wall = top positive gamma → strike 100 (gamma 2.0)
    assert td.call_wall_strike == 100.0
    assert td.call_wall_gamma == 2.0
    # put wall = top negative gamma → strike 99 (gamma -1.5)
    assert td.put_wall_strike == 99.0
    assert td.put_wall_gamma == -1.5
    # gamma_per_1pct: at price 100.5 ±1% = [99.495, 101.505] → strikes 100, 101
    # |2.0| + |1.0| = 3.0
    assert td.gamma_per_1pct == 3.0


def test_deep_true_endpoint_failure_degrades_only_that_field():
    c = _deep_mock_client()
    c.get_iv_rank.side_effect = RuntimeError("upstream 500")
    td = fetch_ticker_data("TSLA", c, deep=True)
    # iv_rank failed, but the rest still populated
    assert td.iv_rank is None
    assert td.iv_52w_low is None
    assert td.price == 100.5
    assert td.sector == "Technology"
    assert td.short_volume_ratio == 0.55
