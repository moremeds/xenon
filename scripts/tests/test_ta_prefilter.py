"""Tests for Stage A TA prefilter."""

from __future__ import annotations

import pytest


def test_score_ma_alignment_full_stack():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_ma_alignment

    assert score_ma_alignment(close=150, ma_20=145, ma_50=140, ma_200=130) == 1.0


def test_score_ma_alignment_partial():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_ma_alignment

    assert score_ma_alignment(close=150, ma_20=145, ma_50=125, ma_200=130) == 0.5


def test_score_ma_alignment_inverted():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_ma_alignment

    assert score_ma_alignment(close=120, ma_20=130, ma_50=140, ma_200=150) == 0.0


def test_score_rsi_constructive():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_rsi

    assert score_rsi(62.0) == 1.0
    assert score_rsi(58.0) == 1.0


def test_score_rsi_outside_range():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_rsi

    assert score_rsi(35.0) < 0.3
    assert score_rsi(85.0) < 0.3


def test_score_adx_strong_trend():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_adx

    assert score_adx(35.0) > 0.7
    assert score_adx(45.0) > 0.9


def test_score_adx_no_trend():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_adx

    assert score_adx(10.0) < 0.3


def test_score_macd_bullish():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_macd

    assert score_macd(macd=1.5, signal=1.0, histogram=0.5) == 1.0


def test_score_macd_bearish():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_macd

    assert score_macd(macd=-1.0, signal=0.5, histogram=-1.5) == 0.0


def test_score_relative_strength_outperforming():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_relative_strength

    assert score_relative_strength(1.15) > 0.7


def test_score_relative_strength_underperforming():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_relative_strength

    assert score_relative_strength(0.85) < 0.3


def test_score_slope_positive():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_slope

    assert score_slope([140, 141, 142, 143, 145]) > 0.7


def test_score_slope_flat():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_slope

    assert score_slope([140, 140, 140, 140, 140]) == 0.5


def test_score_slope_negative():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_slope

    assert score_slope([145, 144, 143, 142, 140]) < 0.3


def test_score_volume_profile_above_avg():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_volume_profile

    assert score_volume_profile(recent_avg_volume=1_500_000, avg_20d_volume=1_000_000, recent_up_ratio=0.7) > 0.7


def test_score_bbw_squeeze():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_bbw

    assert score_bbw(0.03) > 0.7


def test_score_bbw_wide():
    from scripts.trend_scan_lib.stages.ta_prefilter import score_bbw

    assert score_bbw(0.20) < 0.4


def test_breakout_near_52w_high():
    from scripts.trend_scan_lib.stages.ta_prefilter import detect_breakout

    assert detect_breakout(close=148, high_52w=150, range_20d_pct=0.05, atr_pct=0.02) is True


def test_breakout_consolidation_break():
    from scripts.trend_scan_lib.stages.ta_prefilter import detect_breakout

    assert detect_breakout(close=100, high_52w=120, range_20d_pct=0.03, atr_pct=0.015) is True


def test_no_breakout():
    from scripts.trend_scan_lib.stages.ta_prefilter import detect_breakout

    assert detect_breakout(close=100, high_52w=150, range_20d_pct=0.15, atr_pct=0.02) is False


def test_bullish_gate_passes():
    from scripts.trend_scan_lib.stages.ta_prefilter import passes_bullish_gate

    assert (
        passes_bullish_gate(close=150, ma_20=145, rsi=55, dollar_volume=20_000_000, min_dollar_volume=10_000_000)
        is True
    )


def test_bullish_gate_fails_below_ma():
    from scripts.trend_scan_lib.stages.ta_prefilter import passes_bullish_gate

    assert (
        passes_bullish_gate(close=140, ma_20=145, rsi=55, dollar_volume=20_000_000, min_dollar_volume=10_000_000)
        is False
    )


def test_bullish_gate_fails_low_rsi():
    from scripts.trend_scan_lib.stages.ta_prefilter import passes_bullish_gate

    assert (
        passes_bullish_gate(close=150, ma_20=145, rsi=35, dollar_volume=20_000_000, min_dollar_volume=10_000_000)
        is False
    )


def test_bullish_gate_fails_low_volume():
    from scripts.trend_scan_lib.stages.ta_prefilter import passes_bullish_gate

    assert (
        passes_bullish_gate(close=150, ma_20=145, rsi=55, dollar_volume=5_000_000, min_dollar_volume=10_000_000)
        is False
    )


def test_compute_trend_score_strong_trend():
    from scripts.trend_scan_lib.stages.ta_prefilter import compute_trend_score

    indicators = {
        "close": 150,
        "ma_20": 145,
        "ma_50": 140,
        "ma_200": 130,
        "rsi": 62,
        "adx": 32,
        "macd": 1.5,
        "macd_signal": 1.0,
        "macd_histogram": 0.5,
        "rs_vs_spy": 1.15,
        "ma_20_series": [140, 141, 142, 143, 145],
        "recent_avg_volume": 1_500_000,
        "avg_20d_volume": 1_000_000,
        "recent_up_ratio": 0.7,
        "bbw": 0.05,
        "high_52w": 152,
        "range_20d_pct": 0.04,
        "atr_pct": 0.015,
    }
    score = compute_trend_score(indicators)
    assert 0.7 < score <= 1.0


def test_compute_trend_score_weak_trend():
    from scripts.trend_scan_lib.stages.ta_prefilter import compute_trend_score

    indicators = {
        "close": 130,
        "ma_20": 135,
        "ma_50": 140,
        "ma_200": 150,
        "rsi": 38,
        "adx": 12,
        "macd": -1.0,
        "macd_signal": 0.5,
        "macd_histogram": -1.5,
        "rs_vs_spy": 0.85,
        "ma_20_series": [145, 144, 143, 142, 140],
        "recent_avg_volume": 800_000,
        "avg_20d_volume": 1_000_000,
        "recent_up_ratio": 0.3,
        "bbw": 0.18,
        "high_52w": 170,
        "range_20d_pct": 0.12,
        "atr_pct": 0.02,
    }
    score = compute_trend_score(indicators)
    assert score < 0.4
