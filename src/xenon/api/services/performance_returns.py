"""Return-formula library — pure functions, no DB.

Three flavors, picked deliberately:

simple_flow_adjusted_return — retail-intuitive headline. Matches what most
brokers display: "my account is up $X, but $Y of that was deposits".

time_weighted_return — chains daily returns. The denominator each day is
yesterday's NAV; cash flows do NOT directly inflate the numerator (the
caller is responsible for subtracting flow_t in each daily_return). TWR
isolates the manager's compounding skill from the investor's flow timing.

money_weighted_return_irr — solves NPV = 0 for r, weighting cash flows by
when they occurred. Reflects the investor's actual experienced return.
For accounts with no interim flows, IRR ≈ Simple ≈ TWR.

Why all three? Different audiences. Retail users want Simple; finance
folks want TWR or IRR. The /performance tooltip surfaces all three so
the operator can pick the right number for the conversation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CashFlow:
    """Sign convention: positive amount = money OUT of the account (paid back
    to the investor); negative = money IN (investor "pays" into the account).

      opening NAV  → CashFlow(d=t0, amount=-NAV_open)   # investor "pays in"
      deposit      → CashFlow(d=t,  amount=-deposit)     # investor pays in
      withdrawal   → CashFlow(d=t,  amount=+withdrawal)  # investor receives
      closing NAV  → CashFlow(d=tN, amount=+NAV_close)   # account returns capital

    NOTE this is the IRR-side sign convention. The summary-level
    `net_external_flows` uses the opposite (deposits = +, withdrawals = -)
    because that's how the headline arithmetic reads. The caller is
    responsible for the conversion at the boundary.
    """

    d: date
    amount: float


def simple_flow_adjusted_return(*, start: float, end: float, net_flows: float) -> float:
    """``(end - start - net_flows) / start``.

    `net_flows` uses the retail convention: positive = deposit (money IN),
    negative = withdrawal (money OUT). Returns 0.0 when start <= 0 to avoid
    divide-by-zero on cold-start scopes.
    """
    if start <= 0:
        return 0.0
    return (end - start - net_flows) / start


def time_weighted_return(daily_returns: np.ndarray) -> float:
    """``∏(1 + r_i) - 1``. Empty input returns 0.0.

    Caller must compute daily_returns with cash flows already removed from
    the numerator: ``r_t = (NAV_t - NAV_{t-1} - flow_t) / NAV_{t-1}``. See
    ``_futu_returns`` in performance.py for the calling convention.
    """
    if daily_returns is None or len(daily_returns) == 0:
        return 0.0
    return float(np.prod(1.0 + daily_returns) - 1.0)


def money_weighted_return_irr(flows: list[CashFlow]) -> Optional[float]:
    """Solve ``Σ amount_i / (1+r)^(Δt_i / 365.25) = 0`` for r.

    Returns None when:
      - fewer than 2 flows
      - all flows same sign (no NPV sign change → no root)
      - scipy is unavailable
      - brentq fails to converge inside [-0.999, 10.0]

    Day-count convention: 365.25 (handles leap years on average).
    """
    if flows is None or len(flows) < 2:
        return None

    signs = {1 if f.amount > 0 else -1 if f.amount < 0 else 0 for f in flows}
    signs.discard(0)
    if len(signs) < 2:
        return None

    try:
        from scipy.optimize import brentq
    except ImportError:
        logger.warning("scipy unavailable — IRR not computed")
        return None

    t0 = flows[0].d

    def _npv(r: float) -> float:
        total = 0.0
        for f in flows:
            dt_years = (f.d - t0).days / 365.25
            total += f.amount / ((1.0 + r) ** dt_years)
        return total

    try:
        # Bracket wide: -99.9% (near-total loss) to +1000% annualized.
        return float(brentq(_npv, -0.999, 10.0, maxiter=200, xtol=1e-7))
    except (ValueError, RuntimeError) as exc:
        # brentq raises ValueError when the bracket doesn't sandwich a root
        # (rare given the sign check above, but possible for pathological flows).
        logger.warning("IRR brentq failed: %s", exc)
        return None
