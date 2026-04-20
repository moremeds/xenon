from datetime import datetime
from xenon.analysis.models import (
    VRPState, RegimeState, BucketScores, BenchmarkSnapshot, BenchmarkContext,
    TickerData, AnalysisReport,
)


def test_vrp_state_allows_optional_fields():
    s = VRPState(
        vrp_raw=None, vrp_zscore=None, iv_percentile=None,
        ts_ratio=None, ts_inverted=None, earnings_within_14d=True,
        data_freshness="unavailable",
    )
    assert s.earnings_within_14d is True
    assert s.data_freshness == "unavailable"


def test_bucket_scores_requires_mode_and_bias():
    b = BucketScores(
        market_structure=10.0, volatility=-5.0, flow=0.0, positioning=0.0,
        composite=5.0, grade="B", bias="BULLISH",
        mode="full", reweighted=False, skipped_buckets=[],
    )
    assert b.mode == "full"
    assert b.bias == "BULLISH"
    assert b.grade == "B"


def test_regime_state_only_allows_r0_r1_r2():
    r = RegimeState(
        regime="R0", reason="test",
        gex_sign="positive", gex_flip_relative="below_price", flip_distance_pct=1.5,
    )
    assert r.regime == "R0"


def test_ticker_data_bucket_available_rules():
    td_full = TickerData(
        ticker="TSLA", price=200.0, fetched_at=datetime.now(),
        gex={"net": 1.0}, gex_by_strike={"strikes": []},
        iv=30.0, rv=25.0, iv_percentile=60.0, term_structure=[{"iv": 0.3}],
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=[{}], net_premium=None, pcr=None, darkpool=None,
        oi_changes=[{}], short_interest=None,
        earnings_date=None, earnings_within_14d=True,
    )
    assert td_full.bucket_available("market_structure") is True
    assert td_full.bucket_available("volatility") is True
    assert td_full.bucket_available("flow") is True
    # v1 LIMITATION: positioning is always unavailable (OI history deferred).
    assert td_full.bucket_available("positioning") is False

    td_empty = TickerData(
        ticker="X", price=None, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=None, rv=None, iv_percentile=None, term_structure=None,
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=None, net_premium=None, pcr=None, darkpool=None,
        oi_changes=None, short_interest=None,
        earnings_date=None, earnings_within_14d=True,
    )
    assert td_empty.bucket_available("market_structure") is False
    assert td_empty.bucket_available("volatility") is False
    assert td_empty.bucket_available("flow") is False
    assert td_empty.bucket_available("positioning") is False


def test_bucket_available_flow_with_only_options_volume_fields():
    """I1 regression — flow availability must reflect the fields _score_flow
    actually reads. Pre-fix, only `net_premium` (the ticks dict) was checked,
    so a successful options_volume fetch + failed ticks fetch silently
    skipped the entire flow bucket."""
    td = TickerData(
        ticker="X", price=100.0, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=None, rv=None, iv_percentile=None, term_structure=None,
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=None, net_premium=None, pcr=None, darkpool=None,
        oi_changes=None, short_interest=None,
        earnings_date=None, earnings_within_14d=False,
        net_call_premium=1000.0, net_put_premium=-500.0,
    )
    assert td.bucket_available("flow") is True


def test_analysis_report_roundtrip():
    r = AnalysisReport(
        ticker="AAPL", price=180.0, fetched_at="2026-04-07T10:00:00",
        data_freshness={"gex": "live"},
        benchmark=BenchmarkContext(spy=BenchmarkSnapshot(
            ticker="SPY", iv_rank=35.0, gex_regime="positive", gex_flip=450.0,
            price=460.0, data_date="2026-04-07", freshness="live",
        ), sector_etf=None),
        vrp=VRPState(
            vrp_raw=5.0, vrp_zscore=0.8, iv_percentile=55.0,
            ts_ratio=0.95, ts_inverted=False,
            earnings_within_14d=False, data_freshness="live",
        ),
        regime=RegimeState(
            regime="R0", reason="ok", gex_sign="positive",
            gex_flip_relative="below_price", flip_distance_pct=2.0,
        ),
        scores=BucketScores(
            market_structure=15.0, volatility=10.0, flow=12.0, positioning=8.0,
            composite=45.0, grade="A", bias="BULLISH",
            mode="full", reweighted=False, skipped_buckets=[],
        ),
        notes=["test note"],
    )
    assert r.ticker == "AAPL"
    assert r.scores.composite == 45.0
