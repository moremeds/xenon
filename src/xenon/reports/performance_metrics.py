"""Pure-math performance metrics. No I/O, no async, no DB.

Lifted from xenon.reports.portfolio_performance with no semantic change
except for the new `sharpe_se` function (spec §4 — low-confidence indicator
math).
"""
from __future__ import annotations

import math

import numpy as np


def sharpe(returns: np.ndarray, rf: float = 0.0, periods: int = 252) -> float:
    """Annualized Sharpe ratio for daily returns."""
    if len(returns) < 2:
        return 0.0
    excess = returns - rf / periods
    sd = float(np.std(excess, ddof=1))
    if sd == 0.0:
        return 0.0
    return float(np.mean(excess) / sd * math.sqrt(periods))


def sortino(returns: np.ndarray, rf: float = 0.0, periods: int = 252) -> float:
    """Annualized Sortino ratio (downside-deviation in the denominator)."""
    if len(returns) < 2:
        return 0.0
    excess = returns - rf / periods
    downside = excess[excess < 0]
    if len(downside) == 0:
        return 0.0
    dd = float(np.std(downside, ddof=1))
    if dd == 0.0:
        return 0.0
    return float(np.mean(excess) / dd * math.sqrt(periods))


def max_drawdown(equity: np.ndarray) -> tuple[float, int, int]:
    """Return (depth_fraction, duration_sessions, trough_index).

    `depth_fraction` is negative (e.g. -0.15 = 15% drawdown).
    """
    if len(equity) == 0:
        return 0.0, 0, 0
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    trough = int(np.argmin(dd))
    peak_idx = int(np.argmax(equity[: trough + 1]))
    return float(dd[trough]), trough - peak_idx, trough


def beta_alpha(returns: np.ndarray, bench: np.ndarray) -> tuple[float, float]:
    """OLS β + α of returns vs benchmark returns. Skips rows with NaN in either."""
    mask = ~(np.isnan(returns) | np.isnan(bench))
    r, b = returns[mask], bench[mask]
    if len(r) < 2:
        return 0.0, 0.0
    cov = np.cov(r, b, ddof=1)
    var_b = cov[1, 1]
    if var_b == 0.0:
        return 0.0, 0.0
    beta = cov[0, 1] / var_b
    alpha = float(np.mean(r) - beta * np.mean(b))
    return float(beta), alpha


def information_ratio(returns: np.ndarray, bench: np.ndarray) -> tuple[float, float]:
    """Return (IR, tracking_error). IR = mean(diff)/std(diff)."""
    diff = returns - bench
    if len(diff) < 2:
        return 0.0, 0.0
    te = float(np.std(diff, ddof=1))
    if te == 0.0:
        return 0.0, 0.0
    return float(np.mean(diff) / te), te


def upside_downside_capture(
    returns: np.ndarray, bench: np.ndarray
) -> tuple[float, float]:
    """Return (upside_capture, downside_capture) ratios."""
    up_mask = bench > 0
    down_mask = bench < 0

    def _capture(mask):
        if not mask.any():
            return 0.0
        b = float(np.mean(bench[mask]))
        if b == 0.0:
            return 0.0
        return float(np.mean(returns[mask]) / b)

    return _capture(up_mask), _capture(down_mask)


def var_cvar(returns: np.ndarray, percentile: float = 0.05) -> tuple[float, float]:
    """Historical VaR + CVaR at the given lower percentile (default 5%)."""
    if len(returns) == 0:
        return 0.0, 0.0
    var = float(np.quantile(returns, percentile))
    tail = returns[returns <= var]
    cvar = float(np.mean(tail)) if len(tail) > 0 else var
    return var, cvar


def tail_ratio(returns: np.ndarray) -> float:
    """abs(95th pct) / abs(5th pct). > 1.0 means upside tail is fatter."""
    if len(returns) < 20:
        return 0.0
    p95 = float(np.quantile(returns, 0.95))
    p5 = float(np.quantile(returns, 0.05))
    if p5 == 0.0:
        return 0.0
    return abs(p95) / abs(p5)


def ulcer_index(equity: np.ndarray) -> float:
    """Sqrt of mean squared drawdown — captures duration and depth."""
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    dd_pct = ((equity - peak) / peak) * 100
    return float(math.sqrt(np.mean(dd_pct**2)))


def skew(returns: np.ndarray) -> float:
    """Excess skewness (Fisher-Pearson). Pure numpy implementation."""
    if len(returns) < 3:
        return 0.0
    m = float(np.mean(returns))
    sd = float(np.std(returns, ddof=1))
    if sd == 0.0:
        return 0.0
    n = len(returns)
    return float(np.sum((returns - m) ** 3) / (n * sd**3))


def kurtosis(returns: np.ndarray) -> float:
    """Excess kurtosis (subtracts 3). Pure numpy."""
    if len(returns) < 4:
        return 0.0
    m = float(np.mean(returns))
    sd = float(np.std(returns, ddof=1))
    if sd == 0.0:
        return 0.0
    n = len(returns)
    return float(np.sum((returns - m) ** 4) / (n * sd**4)) - 3.0


def hit_rate(returns: np.ndarray) -> float:
    """Fraction of non-zero days that were positive."""
    nonzero = returns[returns != 0]
    if len(nonzero) == 0:
        return 0.0
    return float((nonzero > 0).sum() / len(nonzero))


def sharpe_se(n_sessions: int, periods: int = 252) -> float:
    """Standard error of the Sharpe estimate for daily returns (spec §4).

    SE(Sharpe) ≈ sqrt(periods / n) for daily sampling. Used by the panel to
    render a low-confidence badge tooltip when n < XENON_PERF_LOW_CONFIDENCE_DAYS.
    """
    if n_sessions <= 0:
        raise ValueError(f"n_sessions must be positive, got {n_sessions!r}")
    return float(math.sqrt(periods / n_sessions))
