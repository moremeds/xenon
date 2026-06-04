"""Pure return-formula tests — no DB, no DataFrame."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from xenon.api.services.performance_returns import (
    CashFlow,
    money_weighted_return_irr,
    simple_flow_adjusted_return,
    time_weighted_return,
)


def test_simple_no_flows():
    """100 → 110, no flows = +10%."""
    assert simple_flow_adjusted_return(start=100.0, end=110.0, net_flows=0.0) == pytest.approx(0.10)


def test_simple_with_deposit():
    """100 → 115, +5 deposit → real gain = 10, total_return = +10%."""
    assert simple_flow_adjusted_return(start=100.0, end=115.0, net_flows=5.0) == pytest.approx(0.10)


def test_simple_with_withdrawal():
    """100 → 95, -10 withdrawal → real gain = 5, total_return = +5%."""
    assert simple_flow_adjusted_return(start=100.0, end=95.0, net_flows=-10.0) == pytest.approx(0.05)


def test_simple_zero_start_returns_zero():
    """Cold-start protection: division by zero falls back to 0."""
    assert simple_flow_adjusted_return(start=0.0, end=100.0, net_flows=0.0) == 0.0


def test_twr_no_flows_matches_simple_compounding():
    """Daily returns +1%, +1%, +1% → (1.01)^3 - 1."""
    daily_returns = np.array([0.01, 0.01, 0.01])
    assert time_weighted_return(daily_returns) == pytest.approx((1.01**3) - 1)


def test_twr_isolates_manager_skill():
    """Same daily returns → identical TWR (that's the point of TWR)."""
    daily_returns = np.array([0.01, -0.005, 0.02])
    a = time_weighted_return(daily_returns)
    b = time_weighted_return(daily_returns)
    assert a == b
    assert a == pytest.approx((1.01 * 0.995 * 1.02) - 1)


def test_twr_empty_returns_zero():
    assert time_weighted_return(np.array([])) == 0.0


def test_twr_none_returns_zero():
    """Defensive: None input falls back to 0.0."""
    assert time_weighted_return(None) == 0.0


def test_irr_no_flows_matches_compounding():
    """Single -100 at t0, +110 at t=365d → IRR ≈ 10%."""
    flows = [
        CashFlow(d=date(2025, 1, 1), amount=-100.0),
        CashFlow(d=date(2026, 1, 1), amount=110.0),
    ]
    irr = money_weighted_return_irr(flows)
    assert irr is not None
    assert irr == pytest.approx(0.10, abs=1e-4)


def test_irr_with_intermediate_deposit():
    """Invest 100, deposit 50 at midpoint, end at 165 → solve numerically."""
    flows = [
        CashFlow(d=date(2025, 1, 1), amount=-100.0),
        CashFlow(d=date(2025, 7, 2), amount=-50.0),
        CashFlow(d=date(2026, 1, 1), amount=165.0),
    ]
    irr = money_weighted_return_irr(flows)
    assert irr is not None
    # Verify by re-substituting into NPV equation.
    t0 = date(2025, 1, 1)
    npv = sum(f.amount / ((1 + irr) ** ((f.d - t0).days / 365.25)) for f in flows)
    assert abs(npv) < 1e-3


def test_irr_returns_none_when_no_sign_change():
    """All flows positive → no NPV sign change → no IRR root."""
    flows = [
        CashFlow(d=date(2025, 1, 1), amount=100.0),
        CashFlow(d=date(2026, 1, 1), amount=110.0),
    ]
    assert money_weighted_return_irr(flows) is None


def test_irr_returns_none_for_too_few_flows():
    """Need at least 2 flows."""
    assert money_weighted_return_irr([CashFlow(d=date(2025, 1, 1), amount=-100.0)]) is None
    assert money_weighted_return_irr([]) is None
    assert money_weighted_return_irr(None) is None  # type: ignore[arg-type]


def test_irr_signs_ignores_zero_amounts():
    """Zero-amount entries shouldn't make a same-sign flow set look opposite-sign."""
    flows = [
        CashFlow(d=date(2025, 1, 1), amount=100.0),
        CashFlow(d=date(2025, 6, 1), amount=0.0),
        CashFlow(d=date(2026, 1, 1), amount=110.0),
    ]
    assert money_weighted_return_irr(flows) is None
