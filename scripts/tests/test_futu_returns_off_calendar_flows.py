"""``_futu_returns`` must roll off-calendar flows to the next curve date.

The IB Flex CashTransactions section often reports deposits/withdrawals
with timestamps on weekends or US holidays (banks settle 24/7 even when
exchanges don't). ``load_nav_curve`` only emits trading-day rows, so a
naive exact-date ``reindex(curve.date, fill_value=0)`` silently drops
weekend/holiday flows. The next trading day's NAV jump then includes
the deposit, and the daily return calculation mis-attributes the
deposit-portion as investment performance.

Surfaced by codex-review tribunal during /review-cycle Pass 2. The user-
facing headline now prefers TWR — which is exactly the metric this bug
biases — so it's the most consequential of the four return flavors.

Fix: aggregate flows by the next curve date >= flow date instead of
exact match. Flows past the last curve date are still dropped (no NAV
interval to attribute them to); flows on/before the first curve date
roll into position 0 (which contributes 0 by convention but still
shows up in the net_external_flows total via the separate sum path).
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from xenon.api.services.performance import _futu_returns


def _curve(dates: list[date], navs: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"date": dates, "nav": navs})


def test_weekend_flow_rolls_into_monday_interval():
    """Deposit on Saturday must NOT count as Monday performance.

    Curve: Friday=1000, Monday=2000 (NAV jumped because of the deposit).
    Flow: Saturday +1000 (the deposit).

    Without the roll-forward, Monday's flow_aligned=0 and r_Monday =
    (2000 - 1000 - 0) / 1000 = +100% — totally wrong; the +1000 was a
    deposit, not gain.

    With the roll-forward, Monday's flow_aligned=1000 and r_Monday =
    (2000 - 1000 - 1000) / 1000 = 0 — honest: no trading happened.
    """
    curve = _curve([date(2026, 1, 2), date(2026, 1, 5)], [1000.0, 2000.0])
    flows = pd.Series({date(2026, 1, 3): 1000.0})  # Saturday
    returns = _futu_returns(curve, flows)
    assert returns.shape == (2,)
    assert returns[0] == 0.0  # synthetic
    assert abs(returns[1]) < 1e-9, (
        f"Saturday deposit must roll into Monday's interval — got {returns[1]} "
        "(expected 0 because the +1000 NAV jump was the deposit, not gain)"
    )


def test_trading_day_flow_attributes_correctly():
    """A flow ON a trading day still aligns to that day (no behavior change)."""
    curve = _curve([date(2026, 1, 5), date(2026, 1, 6)], [1000.0, 2000.0])
    flows = pd.Series({date(2026, 1, 6): 1000.0})  # Tuesday
    returns = _futu_returns(curve, flows)
    assert abs(returns[1]) < 1e-9


def test_flow_past_curve_end_is_dropped():
    """Flows after the last curve date have no NAV interval to attribute to."""
    curve = _curve([date(2026, 1, 5), date(2026, 1, 6)], [1000.0, 1100.0])
    flows = pd.Series({date(2026, 1, 9): 500.0})  # Friday, after curve end
    returns = _futu_returns(curve, flows)
    # r_1 = (1100 - 1000 - 0) / 1000 = 0.1 (no flow attributed)
    assert abs(returns[1] - 0.1) < 1e-9


def test_multiple_weekend_flows_aggregate_to_monday():
    """Two flows on Sat + Sun both roll into Monday's interval."""
    curve = _curve([date(2026, 1, 2), date(2026, 1, 5)], [1000.0, 2500.0])
    flows = pd.Series(
        {
            date(2026, 1, 3): 1000.0,  # Sat
            date(2026, 1, 4): 500.0,  # Sun
        }
    )
    returns = _futu_returns(curve, flows)
    # Monday delta = 2500 - 1000 = 1500; rolled flow = 1500 → return = 0
    assert abs(returns[1]) < 1e-9


def test_no_flows_returns_unchanged():
    curve = _curve([date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)], [1000.0, 1010.0, 1030.0])
    returns = _futu_returns(curve, None)
    # r_0 synthetic; r_1 = 10/1000 = 0.01; r_2 = 20/1010 ≈ 0.0198
    assert returns.shape == (3,)
    assert returns[0] == 0.0
    assert abs(returns[1] - 0.01) < 1e-9
    assert abs(returns[2] - (20.0 / 1010.0)) < 1e-9


def test_empty_flows_series_returns_unchanged():
    curve = _curve([date(2026, 1, 5), date(2026, 1, 6)], [1000.0, 1100.0])
    returns = _futu_returns(curve, pd.Series(dtype="float64"))
    assert abs(returns[1] - 0.1) < 1e-9
