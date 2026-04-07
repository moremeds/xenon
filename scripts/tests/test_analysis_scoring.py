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


def test_market_structure_call_wall_above_price_subtracts():
    """Call wall sitting at-the-money acts as a ceiling → bearish tilt."""
    td = _td(
        gex={"net": 1e9}, gex_by_strike={"strikes": []},
        call_wall_strike=100.0,  # at-the-money → max -5
        put_wall_strike=80.0,    # 20% away → 0
    )
    bear_regime = RegimeState(
        regime="R1", reason="t", gex_sign=None, gex_flip_relative=None, flip_distance_pct=None,
    )
    s = score_buckets(td, _VRP, bear_regime, mode="full")
    assert s.market_structure < 0


def test_market_structure_put_wall_below_price_adds():
    td = _td(
        gex={"net": 1e9}, gex_by_strike={"strikes": []},
        call_wall_strike=120.0,  # 20% away → 0
        put_wall_strike=100.0,   # at-the-money → max +5
    )
    neut = RegimeState(
        regime="R1", reason="t", gex_sign=None, gex_flip_relative=None, flip_distance_pct=None,
    )
    s = score_buckets(td, _VRP, neut, mode="full")
    assert s.market_structure > 0


def test_volatility_iv_rank_overrides_iv_percentile_when_present():
    """Deep enrichment iv_rank takes precedence over vrp.iv_percentile fallback."""
    td_low_iv = _td(
        gex={"net": 1e9}, gex_by_strike={"strikes": []},
        iv=30.0, rv=29.0,  # ratio ~0.97 → neutral, doesn't bleed score
        iv_rank=15.0,  # cheap vol → +8
    )
    vrp_no_inv = VRPState(
        vrp_raw=0.0, vrp_zscore=0.0, iv_percentile=80.0,
        ts_ratio=1.0, ts_inverted=False, earnings_within_14d=False,
        data_freshness="live",
    )
    s = score_buckets(td_low_iv, vrp_no_inv, _REGIME, mode="full")
    assert s.volatility >= 8


def test_flow_net_premium_tilt_dominates_when_present():
    td = _td(
        gex={"net": 1e9}, gex_by_strike={"strikes": []},
        net_call_premium=10000.0, net_put_premium=-2000.0,
        flow_alerts=[{"option_type": "call"}],
    )
    s = score_buckets(td, _VRP, _REGIME, mode="full")
    assert s.flow > 5  # strong positive net premium tilt


def test_flow_short_volume_trend_rising_subtracts():
    td = _td(
        gex={"net": 1e9}, gex_by_strike={"strikes": []},
        net_call_premium=0.0, net_put_premium=0.0,
        short_volume_trend=[0.65, 0.55, 0.50],  # newest first → rising
    )
    s = score_buckets(td, _VRP, _REGIME, mode="full")
    assert s.flow < 0


def test_saturation_aliasing_two_strong_fixtures_distinct_composites():
    """Regression: two distinct strong inputs must produce distinct composites,
    not the same clamped value (catches budget overflow / saturation aliasing)."""
    base_kwargs = dict(
        gex={"net": 1e9}, gex_by_strike={"strikes": []},
    )

    # Fixture A: strong bullish on volatility + flow
    td_a = _td(
        **base_kwargs,
        iv_rank=20.0,
        net_call_premium=5000.0, net_put_premium=-1000.0,
        short_volume_trend=[0.40, 0.50, 0.55],  # falling = bullish
    )
    # Fixture B: even stronger across the board
    td_b = _td(
        **base_kwargs,
        iv_rank=10.0,
        net_call_premium=20000.0, net_put_premium=-500.0,
        short_volume_trend=[0.35, 0.50, 0.60],  # falling more
        call_wall_strike=120.0, put_wall_strike=99.0,  # tight floor
        gamma_per_1pct=15.0,
    )
    s_a = score_buckets(td_a, _VRP, _REGIME, mode="full")
    s_b = score_buckets(td_b, _VRP, _REGIME, mode="full")
    assert s_a.composite != s_b.composite, (
        f"saturation alias: {s_a.composite} == {s_b.composite}"
    )


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
