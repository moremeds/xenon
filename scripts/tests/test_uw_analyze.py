from unittest.mock import MagicMock

from scripts.uw_analyze import run_analysis
from scripts.analysis.models import AnalysisReport


def _full_client():
    c = MagicMock()
    c.get_volatility_stats.return_value = {"iv": "0.28", "rv": "0.21", "iv_rank": "0.55"}
    c.get_volatility_term_structure.return_value = {
        "data": [{"dte": 14, "iv": "0.30"}, {"dte": 90, "iv": "0.28"}]
    }
    c.get_greek_exposure.return_value = {"net": 1e9, "flip": 95.0, "price": 100.0}
    c.get_greek_exposure_by_strike.return_value = {"strikes": []}
    c.get_flow_alerts.return_value = {"data": [{}, {}]}
    c.get_darkpool_flow.return_value = {}
    c.get_earnings_by_ticker.return_value = {"data": []}
    c.get_short_data.return_value = {}
    c.get_historical_risk_reversal_skew.return_value = {"data": []}
    c.get_stock_info.return_value = {}
    return c


def test_run_analysis_returns_report():
    client = _full_client()
    report = run_analysis("TSLA", client=client)
    assert isinstance(report, AnalysisReport)
    assert report.ticker == "TSLA"
    assert report.scores.mode == "full"


def test_run_analysis_fast_mode_caps_grade_and_skips_buckets():
    client = _full_client()
    report = run_analysis("TSLA", fast=True, client=client)
    assert report.scores.mode == "fast"
    assert "flow" in report.scores.skipped_buckets
    assert "positioning" in report.scores.skipped_buckets


def test_run_analysis_handles_missing_vol_stats():
    client = _full_client()
    client.get_volatility_stats.return_value = {}
    report = run_analysis("TSLA", client=client)
    assert "volatility" in report.scores.skipped_buckets
    assert report.scores.reweighted is True


def test_run_analysis_populates_setup_thesis():
    client = _full_client()
    report = run_analysis("TSLA", client=client)
    assert report.setup_thesis is not None
    assert "structure_family" in report.setup_thesis
    assert "rationale" in report.setup_thesis
    assert report.setup_thesis["bias"] == report.scores.bias


def test_setup_thesis_no_trade_when_regime_R2():
    """Force vrp.iv_percentile high enough that regime classifies R2 if eligible.
    Simpler: build the thesis directly via internal helper."""
    from scripts.uw_analyze import _build_setup_thesis
    from scripts.analysis.models import TickerData, VRPState, RegimeState, BucketScores
    from datetime import datetime

    td = TickerData(
        ticker="X", price=100.0, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=None, rv=None, iv_percentile=None, term_structure=None,
        rr_skew_25d=None, vrp_history=None, flow_alerts=None,
        net_premium=None, pcr=None, darkpool=None,
        oi_changes=None, short_interest=None,
        earnings_date=None, earnings_within_14d=False,
    )
    vrp = VRPState(
        vrp_raw=None, vrp_zscore=None, iv_percentile=None,
        ts_ratio=None, ts_inverted=None, earnings_within_14d=False,
        data_freshness="unavailable",
    )
    regime = RegimeState(
        regime="R2", reason="risk-off",
        gex_sign=None, gex_flip_relative=None, flip_distance_pct=None,
    )
    scores = BucketScores(
        market_structure=0, volatility=0, flow=0, positioning=0,
        composite=0, grade="C", bias="MIXED", mode="full",
        reweighted=True, skipped_buckets=[],
    )
    thesis = _build_setup_thesis(td, vrp, regime, scores)
    assert thesis["structure_family"] == "no_trade_R2"


def test_run_analysis_reads_sector_from_nested_data():
    """Regression: get_stock_info returns {"data": {"sector": ...}}, not flat."""
    client = _full_client()
    client.get_stock_info.return_value = {"data": {"sector": "Technology"}}
    report = run_analysis("TSLA", client=client)
    # Technology → XLK sector ETF lookup must fire (was broken pre-fix)
    assert report.benchmark.sector_etf is not None
