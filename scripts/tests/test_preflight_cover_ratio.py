"""Cover-ratio parameter on preflight.evaluate / evaluate_combo.

cover_ratio defaults to 1.0 (every existing caller). RegimeGate passes
1.25 on TIER_2 throttle to require 125 shares per short call instead
of 100, tightening Gate 4 without changing its core semantics.
"""

from __future__ import annotations

from decimal import Decimal

from xenon.execution.preflight import (
    ComboPreflightLeg,
    ComboPreflightRequest,
    PortfolioLeg,
    PortfolioPosition,
    PortfolioView,
    PreflightRequest,
    ReasonCode,
    _share_cover_threshold,
    evaluate,
    evaluate_combo,
)


def test_share_cover_threshold_default_is_multiplier():
    assert _share_cover_threshold(100, 1.0) == 100


def test_share_cover_threshold_at_125_pct_is_125_shares():
    assert _share_cover_threshold(100, 1.25) == 125


def test_share_cover_threshold_rounds_up_for_non_integer_product():
    # 100 × 1.33 = 133.0 → 133. 100 × 1.001 = 100.1 → 101 (conservative ceil).
    assert _share_cover_threshold(100, 1.001) == 101


def _spy_short_call_req(quantity: int = 1) -> PreflightRequest:
    return PreflightRequest(
        ticker="SPY",
        security_type="OPT",
        action="SELL",
        quantity=quantity,
        right="C",
        expiry="2026-06-19",
        strike=Decimal("450"),
        limit_price=Decimal("3.00"),
        multiplier=100,
    )


def _portfolio_with_shares(ticker: str, shares: int) -> PortfolioView:
    if shares <= 0:
        return PortfolioView(positions=[])
    return PortfolioView(
        positions=[
            PortfolioPosition(
                ticker=ticker,
                structure_type="Stock",
                contracts=shares,
                legs=[PortfolioLeg(direction="LONG", type="Stock", contracts=shares)],
            )
        ]
    )


def test_default_cover_ratio_accepts_short_call_with_exact_100_shares():
    """Baseline: 100 shares cover 1 short call at default ratio."""
    req = _spy_short_call_req(quantity=1)
    portfolio = _portfolio_with_shares("SPY", 100)
    verdict = evaluate(req, portfolio)
    assert verdict.accept is True


def test_strict_cover_ratio_rejects_short_call_with_only_100_shares():
    """TIER_2 throttle: 100 shares no longer cover 1 short call.

    cover_ratio=1.25 → threshold=125 shares per call. 100 shares < 125
    → 0 cover units → reject.
    """
    req = _spy_short_call_req(quantity=1)
    portfolio = _portfolio_with_shares("SPY", 100)
    verdict = evaluate(req, portfolio, cover_ratio=1.25)
    assert verdict.accept is False
    assert verdict.reason_code == ReasonCode.ETF_CALL_UNCOVERED


def test_strict_cover_ratio_accepts_short_call_with_125_shares():
    """125 shares cover 1 short call at the strict ratio."""
    req = _spy_short_call_req(quantity=1)
    portfolio = _portfolio_with_shares("SPY", 125)
    verdict = evaluate(req, portfolio, cover_ratio=1.25)
    assert verdict.accept is True


def test_strict_cover_ratio_rejects_two_shorts_with_240_shares():
    """240 shares cover 1 short at 125-each (1.92 → floor=1), not 2."""
    req = _spy_short_call_req(quantity=2)
    portfolio = _portfolio_with_shares("SPY", 240)
    verdict = evaluate(req, portfolio, cover_ratio=1.25)
    assert verdict.accept is False
    assert verdict.reason_code == ReasonCode.ETF_CALL_UNCOVERED


def test_strict_cover_ratio_accepts_two_shorts_with_250_shares():
    req = _spy_short_call_req(quantity=2)
    portfolio = _portfolio_with_shares("SPY", 250)
    verdict = evaluate(req, portfolio, cover_ratio=1.25)
    assert verdict.accept is True


def test_strict_cover_ratio_does_not_affect_long_buys():
    """BUY orders never see Gate 4 — cover_ratio is irrelevant."""
    req = PreflightRequest(
        ticker="SPY",
        security_type="OPT",
        action="BUY",
        quantity=1,
        right="C",
        expiry="2026-06-19",
        strike=Decimal("450"),
        limit_price=Decimal("3.00"),
    )
    verdict = evaluate(req, _portfolio_with_shares("SPY", 0), cover_ratio=1.25)
    assert verdict.accept is True


def test_strict_cover_ratio_on_combo_with_uncovered_short_call():
    """Combo with naked short-call leg: 100 shares cover at 1.0, not at 1.25."""
    combo = ComboPreflightRequest(
        ticker="SPY",
        action="BUY",
        quantity=1,
        multiplier=100,
        legs=[
            ComboPreflightLeg(
                expiry="2026-06-19",
                strike=Decimal("450"),
                right="C",
                action="SELL",
                ratio=1,
            ),
        ],
    )
    portfolio = _portfolio_with_shares("SPY", 100)
    # Default ratio: accept (100 shares cover 1 short)
    assert evaluate_combo(combo, portfolio).accept is True
    # Strict ratio: reject (100 < 125)
    verdict = evaluate_combo(combo, portfolio, cover_ratio=1.25)
    assert verdict.accept is False
    assert verdict.reason_code == ReasonCode.ETF_CALL_UNCOVERED


def test_strict_cover_ratio_does_not_affect_vertical_spread_close():
    """Vertical: long-call cover absorbs the short, share-cover doesn't apply."""
    combo = ComboPreflightRequest(
        ticker="SPY",
        action="BUY",
        quantity=1,
        multiplier=100,
        legs=[
            ComboPreflightLeg(
                expiry="2026-06-19",
                strike=Decimal("450"),
                right="C",
                action="BUY",
                ratio=1,
            ),
            ComboPreflightLeg(
                expiry="2026-06-19",
                strike=Decimal("460"),
                right="C",
                action="SELL",
                ratio=1,
            ),
        ],
    )
    # No shares needed — long call covers the short. Both ratios accept.
    assert evaluate_combo(combo, PortfolioView(positions=[]), cover_ratio=1.0).accept is True
    assert evaluate_combo(combo, PortfolioView(positions=[]), cover_ratio=1.25).accept is True
