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


def test_run_analysis_reads_sector_from_nested_data():
    """Regression: get_stock_info returns {"data": {"sector": ...}}, not flat."""
    client = _full_client()
    client.get_stock_info.return_value = {"data": {"sector": "Technology"}}
    report = run_analysis("TSLA", client=client)
    # Technology → XLK sector ETF lookup must fire (was broken pre-fix)
    assert report.benchmark.sector_etf is not None
