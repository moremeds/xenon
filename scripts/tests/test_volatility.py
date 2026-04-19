"""Tests for Stage B volatility state scoring."""

from __future__ import annotations

import pytest


def test_score_iv_rank_low():
    from scripts.scanners.trend.stages.volatility import score_iv_rank

    assert score_iv_rank(20) == 1.0


def test_score_iv_rank_moderate():
    from scripts.scanners.trend.stages.volatility import score_iv_rank

    assert score_iv_rank(40) == 0.7


def test_score_iv_rank_high():
    from scripts.scanners.trend.stages.volatility import score_iv_rank

    assert score_iv_rank(60) == 0.4


def test_score_iv_rank_extreme():
    from scripts.scanners.trend.stages.volatility import score_iv_rank

    assert score_iv_rank(85) == 0.2


def test_score_term_structure_normal():
    from scripts.scanners.trend.stages.volatility import score_term_structure

    assert score_term_structure("normal") == 1.0


def test_score_term_structure_flat():
    from scripts.scanners.trend.stages.volatility import score_term_structure

    assert score_term_structure("flat") == 0.6


def test_score_term_structure_inverted():
    from scripts.scanners.trend.stages.volatility import score_term_structure

    assert score_term_structure("inverted") == 0.3


def test_score_iv_rv_ratio_cheap():
    from scripts.scanners.trend.stages.volatility import score_iv_rv_ratio

    assert score_iv_rv_ratio(0.9) > 0.7


def test_score_iv_rv_ratio_expensive():
    from scripts.scanners.trend.stages.volatility import score_iv_rv_ratio

    assert score_iv_rv_ratio(1.5) < 0.4


def test_score_iv_rv_ratio_zero_rv():
    from scripts.scanners.trend.stages.volatility import score_iv_rv_ratio

    assert score_iv_rv_ratio(0) == 0.5


def test_compute_vol_score():
    from scripts.scanners.trend.stages.volatility import compute_vol_score

    data = {"iv_rank": 22, "term_structure": "normal", "iv_rv_ratio": 0.94}
    score, flags = compute_vol_score(data)
    assert score > 0.7
    assert flags == []


def test_compute_vol_score_event_flag():
    from scripts.scanners.trend.stages.volatility import compute_vol_score

    data = {"iv_rank": 65, "term_structure": "inverted", "iv_rv_ratio": 1.3, "earnings_days": 5}
    score, flags = compute_vol_score(data)
    assert score < 0.5
    assert "event_premium" in flags


def test_suggest_trade_type_cheap():
    from scripts.scanners.trend.stages.volatility import suggest_trade_type

    assert suggest_trade_type(iv_rank=20, term_structure="normal", capped=False) == "debit_call"


def test_suggest_trade_type_moderate():
    from scripts.scanners.trend.stages.volatility import suggest_trade_type

    assert suggest_trade_type(iv_rank=45, term_structure="normal", capped=True) == "call_spread"


def test_suggest_trade_type_expensive():
    from scripts.scanners.trend.stages.volatility import suggest_trade_type

    assert suggest_trade_type(iv_rank=70, term_structure="inverted", capped=True) == "premium_sell"
