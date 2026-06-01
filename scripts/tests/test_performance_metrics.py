"""Pure-math tests for xenon.reports.performance_metrics (spec § metrics)."""
import math

import numpy as np
import pytest

from xenon.reports.performance_metrics import (
    beta_alpha,
    hit_rate,
    information_ratio,
    kurtosis,
    max_drawdown,
    sharpe,
    sharpe_se,
    skew,
    sortino,
    tail_ratio,
    ulcer_index,
    upside_downside_capture,
    var_cvar,
)


# ---------- sharpe / sortino ----------


def test_sharpe_positive_drift_gives_positive_value():
    rng = np.random.default_rng(0)
    returns = np.full(252, 0.001) + rng.normal(0, 0.01, 252)
    assert sharpe(returns) > 0.5


def test_sharpe_zero_returns_zero():
    assert sharpe(np.zeros(252)) == 0.0


def test_sortino_punishes_only_downside():
    # All positive returns → no downside → 0 (by convention here).
    assert sortino(np.full(50, 0.01)) == 0.0


# ---------- max drawdown ----------


def test_max_drawdown_simple():
    equity = np.array([100, 110, 105, 95, 100])
    depth, duration, _ = max_drawdown(equity)
    assert depth == pytest.approx((95 - 110) / 110, rel=1e-9)
    assert duration == 2


def test_max_drawdown_monotonic_zero():
    equity = np.arange(10, dtype=float) + 100
    depth, dur, _ = max_drawdown(equity)
    assert depth == 0.0
    assert dur == 0


# ---------- beta / alpha / IR / capture ----------


def test_beta_alpha_identity():
    """returns == bench → beta ≈ 1, alpha ≈ 0."""
    rng = np.random.default_rng(1)
    bench = rng.normal(0, 0.01, 200)
    beta, alpha = beta_alpha(bench, bench)
    assert beta == pytest.approx(1.0, rel=1e-6)
    assert alpha == pytest.approx(0.0, abs=1e-9)


def test_information_ratio_zero_diff():
    bench = np.array([0.01, -0.005, 0.0, 0.02])
    ir, te = information_ratio(bench, bench)
    assert ir == 0.0
    assert te == 0.0


def test_upside_downside_capture_identity():
    """returns == bench → both captures = 1.0."""
    bench = np.array([0.02, -0.01, 0.005, -0.015, 0.01])
    up, down = upside_downside_capture(bench, bench)
    assert up == pytest.approx(1.0, rel=1e-9)
    assert down == pytest.approx(1.0, rel=1e-9)


# ---------- var / cvar / tails ----------


def test_var_cvar_signs():
    rng = np.random.default_rng(2)
    returns = rng.normal(0, 0.02, 1000)
    v, c = var_cvar(returns, 0.05)
    assert v < 0
    assert c <= v  # CVaR always ≤ VaR for the left tail


def test_tail_ratio_zero_for_small_samples():
    assert tail_ratio(np.array([0.01, -0.01, 0.02])) == 0.0


def test_ulcer_index_zero_for_monotonic():
    eq = np.arange(10, dtype=float) + 100
    assert ulcer_index(eq) == 0.0


# ---------- distribution ----------


def test_skew_kurtosis_of_normal_close_to_zero():
    rng = np.random.default_rng(3)
    sample = rng.normal(0, 1, 5000)
    assert abs(skew(sample)) < 0.2
    assert abs(kurtosis(sample)) < 0.4


def test_hit_rate_ignores_zeros():
    returns = np.array([0.0, 0.01, -0.01, 0.0, 0.02])
    # 2 positive / 3 non-zero
    assert hit_rate(returns) == pytest.approx(2 / 3, rel=1e-9)


# ---------- sharpe_se (spec §4) ----------


def test_sharpe_se_canonical_values():
    """SE ≈ sqrt(periods/n). Verified manually for n=30, 60, 126, 252."""
    assert sharpe_se(30) == pytest.approx(math.sqrt(252 / 30), rel=1e-9)
    assert sharpe_se(60) == pytest.approx(math.sqrt(252 / 60), rel=1e-9)
    assert sharpe_se(126) == pytest.approx(math.sqrt(252 / 126), rel=1e-9)
    assert sharpe_se(252) == pytest.approx(1.0, rel=1e-9)


def test_sharpe_se_rejects_zero_or_negative():
    with pytest.raises(ValueError):
        sharpe_se(0)
    with pytest.raises(ValueError):
        sharpe_se(-5)


def test_sharpe_se_monotonic_decrease():
    """More samples → smaller SE (more confident)."""
    assert sharpe_se(30) > sharpe_se(60) > sharpe_se(126) > sharpe_se(252)
