from datetime import datetime
from scripts.analysis.models import TickerData, VRPState, RegimeState
from scripts.analysis.scoring import score_buckets, BUCKET_WEIGHTS


def _td(**kwargs):
    defaults = dict(
        ticker="X", price=100.0, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=30.0, rv=22.0, iv_percentile=60.0,
        term_structure=[{"dte": 14, "iv": "0.30"}, {"dte": 90, "iv": "0.28"}],
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=[], net_premium=None, pcr=1.0, darkpool=None,
        oi_changes=[], short_interest=None,
        earnings_date=None, earnings_within_14d=False,
    )
    defaults.update(kwargs)
    return TickerData(**defaults)


_VRP = VRPState(
    vrp_raw=8.0, vrp_zscore=1.0, iv_percentile=60.0,
    ts_ratio=1.07, ts_inverted=True, earnings_within_14d=False,
    data_freshness="live",
)
_REGIME = RegimeState(
    regime="R1", reason="test",
    gex_sign="positive", gex_flip_relative="below_price", flip_distance_pct=1.0,
)


def test_bucket_weights_sum_to_100():
    assert sum(BUCKET_WEIGHTS.values()) == 100


def test_full_mode_composite_in_range():
    td = _td(gex={"net": 1e9, "flip": 95.0}, gex_by_strike={"strikes": []})
    scores = score_buckets(td, _VRP, _REGIME, mode="full")
    assert scores.mode == "full"
    assert -100 <= scores.composite <= 100
    assert scores.bias in (
        "STRONGLY_BULLISH", "BULLISH", "MIXED", "BEARISH", "STRONGLY_BEARISH"
    )


def test_fast_mode_skips_flow_and_positioning():
    td = _td(gex={"net": 1e9}, gex_by_strike={"strikes": []})
    scores = score_buckets(td, _VRP, _REGIME, mode="fast")
    assert scores.mode == "fast"
    assert scores.reweighted is True
    assert "flow" in scores.skipped_buckets
    assert "positioning" in scores.skipped_buckets


def test_fast_mode_caps_grade_at_b():
    td = _td(gex={"net": 1e9}, gex_by_strike={"strikes": []})
    scores = score_buckets(td, _VRP, _REGIME, mode="fast")
    assert scores.grade in ("B", "C")


def test_bucket_failure_reweights():
    td = _td(gex=None, gex_by_strike=None)
    scores = score_buckets(td, _VRP, _REGIME, mode="full")
    assert "market_structure" in scores.skipped_buckets
    assert "positioning" in scores.skipped_buckets
    assert scores.reweighted is True


def test_bias_mapping_boundaries():
    from scripts.analysis.scoring import score_to_bias
    assert score_to_bias(75.0) == "STRONGLY_BULLISH"
    assert score_to_bias(60.0) == "STRONGLY_BULLISH"
    assert score_to_bias(59.9) == "BULLISH"
    assert score_to_bias(20.0) == "BULLISH"
    assert score_to_bias(19.9) == "MIXED"
    assert score_to_bias(0.0) == "MIXED"
    assert score_to_bias(-19.9) == "MIXED"
    assert score_to_bias(-20.0) == "BEARISH"
    assert score_to_bias(-60.0) == "STRONGLY_BEARISH"
