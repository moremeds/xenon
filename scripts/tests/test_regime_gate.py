"""Unit tests for RegimeGate.veto, _is_hedge, _max_loss_usd.

Pure-function tests — no DB, no HTTP. Cross-product across binding_tier
and order shapes per spec §7.1.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Optional

import pytest

from xenon.api.services.regime_gate import (
    GateDecision,
    RegimeGate,
    _is_hedge,
    _max_loss_usd,
)
from xenon.api.services.regime_state import RegimeState
from xenon.execution.preflight import (
    ComboPreflightLeg,
    ComboPreflightRequest,
    PreflightRequest,
)


def _state(binding: str, side: str = "vcg") -> RegimeState:
    return RegimeState(
        vcg_tier=binding,
        cri_tier="NORMAL",
        binding_tier=binding,
        binding_side=side,
        vcg_scanned_at=None,
        cri_scanned_at=None,
        is_stale=False,
        panic_active=binding == "PANIC",
    )


def _long_call(ticker: str = "AAPL", limit: str = "5.00") -> PreflightRequest:
    return PreflightRequest(
        ticker=ticker,
        security_type="OPT",
        action="BUY",
        quantity=1,
        right="C",
        expiry="2026-06-19",
        strike=Decimal("200"),
        limit_price=Decimal(limit),
    )


def _long_put(ticker: str = "SPY", limit: str = "3.00") -> PreflightRequest:
    return PreflightRequest(
        ticker=ticker,
        security_type="OPT",
        action="BUY",
        quantity=1,
        right="P",
        expiry="2026-06-19",
        strike=Decimal("400"),
        limit_price=Decimal(limit),
    )


def _short_put(ticker: str = "SPY") -> PreflightRequest:
    return PreflightRequest(
        ticker=ticker,
        security_type="OPT",
        action="SELL",
        quantity=1,
        right="P",
        expiry="2026-06-19",
        strike=Decimal("400"),
        limit_price=Decimal("3.00"),
    )


# ---- veto decision-tree cross product -----------------------------------


@pytest.mark.parametrize(
    "tier,is_hedge,expected_decision",
    [
        # NORMAL — never blocks, never throttles, hedge or non-hedge
        ("NORMAL", False, GateDecision.OK),
        ("NORMAL", True, GateDecision.OK),
        # EDR / UNKNOWN — soft throttle for both
        ("EDR", False, GateDecision.THROTTLE),
        ("EDR", True, GateDecision.THROTTLE),
        ("UNKNOWN", False, GateDecision.THROTTLE),
        ("UNKNOWN", True, GateDecision.THROTTLE),
        # TIER_2 — strict throttle, hedge does not bypass
        ("TIER_2", False, GateDecision.THROTTLE),
        ("TIER_2", True, GateDecision.THROTTLE),
        # TIER_1 — block non-hedge, hedge passes
        ("TIER_1", False, GateDecision.BLOCK),
        ("TIER_1", True, GateDecision.OK),
        # PANIC — same as TIER_1
        ("PANIC", False, GateDecision.BLOCK),
        ("PANIC", True, GateDecision.OK),
    ],
)
def test_veto_decision_tree(tier: str, is_hedge: bool, expected_decision: GateDecision):
    order = _long_put("SPY") if is_hedge else _long_call("AAPL")
    state = _state(tier)
    result = RegimeGate.veto(order, state, bankroll_usd=100_000.0)
    assert result.decision == expected_decision


def test_throttle_strict_uses_higher_cover_ratio():
    state = _state("TIER_2")
    result = RegimeGate.veto(_long_call(), state, bankroll_usd=100_000.0)
    assert result.cover_ratio == 1.25
    assert result.max_loss_cap_usd == pytest.approx(1250.0)


def test_throttle_soft_uses_baseline_cover_ratio():
    state = _state("EDR")
    result = RegimeGate.veto(_long_call(), state, bankroll_usd=100_000.0)
    assert result.cover_ratio == 1.0
    assert result.max_loss_cap_usd == pytest.approx(1250.0)


def test_throttle_unknown_uses_baseline_cover_ratio():
    state = _state("UNKNOWN")
    result = RegimeGate.veto(_long_call(), state, bankroll_usd=100_000.0)
    assert result.cover_ratio == 1.0


def test_block_carries_tier_in_reason():
    state = _state("TIER_1")
    result = RegimeGate.veto(_long_call("NVDA"), state, bankroll_usd=100_000.0)
    assert "TIER_1" in result.reason
    assert "non-hedge" in result.reason


def test_binding_side_propagates_to_result():
    state = _state("TIER_2", side="cri")
    result = RegimeGate.veto(_long_call(), state, bankroll_usd=100_000.0)
    assert result.bind == "cri"


# ---- _is_hedge predicate ------------------------------------------------


@pytest.mark.parametrize(
    "ticker,right,action,expected",
    [
        # Long puts on hedge underlyings
        ("SPY", "P", "BUY", True),
        ("SPX", "P", "BUY", True),
        ("HYG", "P", "BUY", True),
        ("JNK", "P", "BUY", True),
        ("LQD", "P", "BUY", True),
        # Long calls on VIX
        ("VIX", "C", "BUY", True),
        # Wrong direction — short = not a hedge
        ("SPY", "P", "SELL", False),
        ("VIX", "C", "SELL", False),
        # Wrong right
        ("SPY", "C", "BUY", False),
        ("VIX", "P", "BUY", False),
        # Non-hedge underlying
        ("AAPL", "P", "BUY", False),
        ("NVDA", "C", "BUY", False),
    ],
)
def test_is_hedge_single_leg(ticker: str, right: str, action: str, expected: bool):
    order = PreflightRequest(
        ticker=ticker,
        security_type="OPT",
        action=action,
        quantity=1,
        right=right,
        expiry="2026-06-19",
        strike=Decimal("100"),
        limit_price=Decimal("2.00"),
    )
    assert _is_hedge(order) is expected


def test_is_hedge_stock_is_never_hedge():
    order = PreflightRequest(
        ticker="SPY",
        security_type="STK",
        action="BUY",
        quantity=100,
        limit_price=Decimal("450"),
    )
    assert _is_hedge(order) is False


def _put_spread_combo(ticker: str, action: str = "BUY") -> ComboPreflightRequest:
    return ComboPreflightRequest(
        ticker=ticker,
        action=action,
        quantity=1,
        multiplier=100,
        legs=[
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("400"), right="P", action="BUY", ratio=1),
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("390"), right="P", action="SELL", ratio=1),
        ],
    )


def test_is_hedge_combo_debit_put_spread_on_spy():
    assert _is_hedge(_put_spread_combo("SPY")) is True


def test_is_hedge_combo_credit_put_spread_is_not_hedge():
    # SELL action = credit combo; writing risk on a hedge underlying is
    # not a hedge.
    assert _is_hedge(_put_spread_combo("SPY", action="SELL")) is False


def test_is_hedge_combo_call_spread_on_aapl_not_hedge():
    combo = ComboPreflightRequest(
        ticker="AAPL",
        action="BUY",
        quantity=1,
        multiplier=100,
        legs=[
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("200"), right="C", action="BUY", ratio=1),
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("210"), right="C", action="SELL", ratio=1),
        ],
    )
    assert _is_hedge(combo) is False


def test_is_hedge_combo_vix_call_spread_is_hedge():
    combo = ComboPreflightRequest(
        ticker="VIX",
        action="BUY",
        quantity=1,
        multiplier=100,
        legs=[
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("20"), right="C", action="BUY", ratio=1),
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("30"), right="C", action="SELL", ratio=1),
        ],
    )
    assert _is_hedge(combo) is True


def test_is_hedge_combo_mixed_expiries_not_hedge():
    combo = ComboPreflightRequest(
        ticker="SPY",
        action="BUY",
        quantity=1,
        multiplier=100,
        legs=[
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("400"), right="P", action="BUY", ratio=1),
            ComboPreflightLeg(expiry="2026-09-18", strike=Decimal("390"), right="P", action="SELL", ratio=1),
        ],
    )
    assert _is_hedge(combo) is False


def test_is_hedge_combo_three_legs_not_hedge():
    combo = ComboPreflightRequest(
        ticker="SPY",
        action="BUY",
        quantity=1,
        multiplier=100,
        legs=[
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("400"), right="P", action="BUY", ratio=1),
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("390"), right="P", action="SELL", ratio=1),
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("380"), right="P", action="BUY", ratio=1),
        ],
    )
    # Three-leg structures don't match the documented hedge set
    assert _is_hedge(combo) is False


# ---- _max_loss_usd ------------------------------------------------------


def test_max_loss_long_option_is_premium_times_size():
    order = _long_call(limit="4.50")
    order = PreflightRequest(
        ticker="AAPL",
        security_type="OPT",
        action="BUY",
        quantity=3,
        right="C",
        expiry="2026-06-19",
        strike=Decimal("200"),
        limit_price=Decimal("4.50"),
        multiplier=100,
    )
    assert _max_loss_usd(order) == pytest.approx(1350.0)


def test_max_loss_short_option_is_inf():
    order = _short_put()
    assert _max_loss_usd(order) == math.inf


def test_max_loss_stock_is_inf():
    order = PreflightRequest(
        ticker="SPY",
        security_type="STK",
        action="BUY",
        quantity=100,
        limit_price=Decimal("450"),
    )
    assert _max_loss_usd(order) == math.inf


def test_max_loss_combo_buy_debit():
    combo = ComboPreflightRequest(
        ticker="AAPL",
        action="BUY",
        quantity=2,
        multiplier=100,
        legs=[
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("200"), right="C", action="BUY", ratio=1),
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("210"), right="C", action="SELL", ratio=1),
        ],
    )
    # net_debit 3.00 × 2 × 100 = 600
    assert _max_loss_usd(combo, net_price=Decimal("3.00")) == pytest.approx(600.0)


def test_max_loss_combo_sell_credit_is_width_minus_credit():
    combo = ComboPreflightRequest(
        ticker="AAPL",
        action="SELL",
        quantity=1,
        multiplier=100,
        legs=[
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("200"), right="C", action="SELL", ratio=1),
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("210"), right="C", action="BUY", ratio=1),
        ],
    )
    # width=10, credit=2, max_loss = (10-2) × 1 × 100 = 800
    assert _max_loss_usd(combo, net_price=Decimal("-2.00")) == pytest.approx(800.0)


def test_max_loss_combo_sell_without_net_price_is_inf():
    combo = ComboPreflightRequest(
        ticker="AAPL",
        action="SELL",
        quantity=1,
        multiplier=100,
        legs=[
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("200"), right="C", action="SELL", ratio=1),
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("210"), right="C", action="BUY", ratio=1),
        ],
    )
    assert _max_loss_usd(combo, net_price=None) == math.inf


def test_max_loss_combo_buy_with_negative_net_price_is_inf():
    # Sign-convention drift — BUY combo cannot be a credit.
    combo = ComboPreflightRequest(
        ticker="AAPL",
        action="BUY",
        quantity=1,
        multiplier=100,
        legs=[
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("200"), right="C", action="BUY", ratio=1),
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("210"), right="C", action="SELL", ratio=1),
        ],
    )
    assert _max_loss_usd(combo, net_price=Decimal("-1.00")) == math.inf


def test_max_loss_iron_condor_uses_max_pair_width():
    combo = ComboPreflightRequest(
        ticker="AAPL",
        action="SELL",
        quantity=1,
        multiplier=100,
        legs=[
            # Call wing — width 5
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("210"), right="C", action="SELL", ratio=1),
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("215"), right="C", action="BUY", ratio=1),
            # Put wing — width 10 (the binding side)
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("190"), right="P", action="SELL", ratio=1),
            ComboPreflightLeg(expiry="2026-06-19", strike=Decimal("180"), right="P", action="BUY", ratio=1),
        ],
    )
    # Net credit 3.00 → max_loss = (10 - 3) × 1 × 100 = 700 (uses larger wing)
    assert _max_loss_usd(combo, net_price=Decimal("-3.00")) == pytest.approx(700.0)


# ---- veto + max_loss integration via order route contract --------------


def test_throttle_cap_at_125pct_of_bankroll():
    """1.25% of $50k bankroll = $625 cap."""
    state = _state("TIER_2")
    result = RegimeGate.veto(_long_call(), state, bankroll_usd=50_000.0)
    assert result.max_loss_cap_usd == pytest.approx(625.0)
