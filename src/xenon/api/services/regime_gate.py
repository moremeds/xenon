"""RegimeGate — order-path veto driven by RegimeState.

Spec: docs/superpowers/specs/2026-04-29-vcg-cri-strategies-rewiring-design.md
§4.5, §4.5.1, §4.6.

Decision flow:

    binding_tier  +  is_hedge  →  GateDecision  (+ throttle parameters)

The (binding_tier → action) map is held in `_TIER_ACTION_MAP` at the top
of the module so future CRI recalibration can be tuned without touching
the order-route integration. `veto()` is a pure function over the
(order, state, bankroll) triple — no DB, no HTTP — so it's trivially
unit-testable.

`_max_loss_usd` returns `math.inf` for anything not classified as
defined-risk. The order route maps that to HTTP 422 resize_required;
naked-short structures should already be rejected by Gate 4 before this
gate runs, but inf is the belt-and-suspenders default.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional, Union

from xenon.api.services.regime_state import RegimeState, TierLabel
from xenon.execution.preflight import ComboPreflightRequest, PreflightRequest


class GateDecision(Enum):
    OK = "ok"
    THROTTLE = "throttle"
    BLOCK = "block"


@dataclass(frozen=True)
class GateResult:
    decision: GateDecision
    reason: str
    bind: str
    # Throttle-only — None / 0.0 outside THROTTLE.
    max_loss_cap_usd: Optional[float] = None
    cover_ratio: Optional[float] = None


# Hedge structure set per spec §4.5. Long-only / defined-risk debit
# verticals on these underlyings count as hedges. Naked shorts on these
# underlyings DO NOT count — naked structures are rejected by Gate 4
# before the regime gate even runs.
_HEDGE_PUT_UNDERLYINGS: frozenset[str] = frozenset({"HYG", "JNK", "LQD", "SPX", "SPY"})
_HEDGE_CALL_UNDERLYINGS: frozenset[str] = frozenset({"VIX"})


# Throttle cap is 1.25% of bankroll = 0.0125. Halved per-order risk
# budget per §3.2.1 (full budget is 2.5% of bankroll per Gate 3 in
# CLAUDE.md; throttle halves it). Cover-ratio bumps to 1.25 only on
# TIER_2 strict throttle — EDR / UNKNOWN keep 1.0.
_THROTTLE_CAP_PCT = 0.0125

# Config-driven map — when CRI gets recalibrated and tier distributions
# shift, tune this table without touching veto() or the order route.
# Format: tier → (decision, cap_pct, cover_ratio).  None for cap/cover
# when the decision is not THROTTLE.
_TIER_ACTION_MAP: dict[TierLabel, tuple[GateDecision, Optional[float], Optional[float]]] = {
    "PANIC": (GateDecision.BLOCK, None, None),
    "TIER_1": (GateDecision.BLOCK, None, None),
    "TIER_2": (GateDecision.THROTTLE, _THROTTLE_CAP_PCT, 1.25),
    "EDR": (GateDecision.THROTTLE, _THROTTLE_CAP_PCT, 1.0),
    "UNKNOWN": (GateDecision.THROTTLE, _THROTTLE_CAP_PCT, 1.0),
    "NORMAL": (GateDecision.OK, None, None),
}


OrderLike = Union[PreflightRequest, ComboPreflightRequest]


class RegimeGate:
    @staticmethod
    def veto(
        order: OrderLike,
        state: RegimeState,
        bankroll_usd: float,
        *,
        net_price: Optional[Decimal] = None,
    ) -> GateResult:
        """Apply the regime gate to a new-exposure order request.

        `net_price` is the combo limit (debit positive, credit negative
        by sign convention). For single-leg `PreflightRequest` it is
        ignored — `order.limit_price` is used directly.
        """
        tier = state.binding_tier
        is_hedge = _is_hedge(order)

        # Step 1: TIER_1 / PANIC blocks non-hedge entries. Hedges at
        # these tiers fall through to OK — invariant 4 from §3.1.
        if tier in ("TIER_1", "PANIC") and not is_hedge:
            return GateResult(
                decision=GateDecision.BLOCK,
                reason=f"{tier} — non-hedge entries blocked",
                bind=state.binding_side,
            )

        decision, cap_pct, cover_ratio = _TIER_ACTION_MAP[tier]

        # Hedge bypass at TIER_1 / PANIC (step 1 didn't BLOCK because
        # is_hedge=True). Surface as OK rather than throttle — hedges
        # never need sizing discipline at panic tiers.
        if tier in ("TIER_1", "PANIC") and is_hedge:
            return GateResult(
                decision=GateDecision.OK,
                reason="",
                bind=state.binding_side,
            )

        if decision is GateDecision.THROTTLE:
            assert cap_pct is not None and cover_ratio is not None
            return GateResult(
                decision=GateDecision.THROTTLE,
                reason=f"{tier} — throttled, halved per-order risk cap",
                bind=state.binding_side,
                max_loss_cap_usd=cap_pct * bankroll_usd,
                cover_ratio=cover_ratio,
            )

        return GateResult(
            decision=GateDecision.OK,
            reason="",
            bind=state.binding_side,
        )


def _is_hedge(order: OrderLike) -> bool:
    """True iff `order` is a long-only / defined-risk hedge structure.

    Single-leg: long put on equity-index/credit hedge underlying, OR
    long call on VIX.
    Combo: all-debit (BUY action) put-spread on HYG/JNK/LQD/SPX/SPY OR
    all-debit call-spread on VIX, both legs same right + same expiry,
    no naked short legs.
    """
    if isinstance(order, PreflightRequest):
        return _is_hedge_single(order)
    if isinstance(order, ComboPreflightRequest):
        return _is_hedge_combo(order)
    return False


def _is_hedge_single(order: PreflightRequest) -> bool:
    if order.security_type != "OPT" or order.action != "BUY":
        return False
    ticker = order.ticker.upper()
    if order.right == "P" and ticker in _HEDGE_PUT_UNDERLYINGS:
        return True
    if order.right == "C" and ticker in _HEDGE_CALL_UNDERLYINGS:
        return True
    return False


def _is_hedge_combo(order: ComboPreflightRequest) -> bool:
    if order.action != "BUY":
        # Credit combo on a hedge underlying is not a hedge — it's
        # writing risk on the same name. Hedges must be debit.
        return False
    ticker = order.ticker.upper()
    if not order.legs:
        return False
    rights = {leg.right for leg in order.legs}
    expiries = {leg.expiry for leg in order.legs}
    if len(rights) != 1 or len(expiries) != 1:
        # Mixed-right or mixed-expiry combos (e.g. straddles, calendars)
        # are not hedges in the §4.5 set.
        return False
    right = next(iter(rights))
    if right == "P" and ticker not in _HEDGE_PUT_UNDERLYINGS:
        return False
    if right == "C" and ticker not in _HEDGE_CALL_UNDERLYINGS:
        return False
    # Two-leg vertical: one BUY + one SELL, same right, same expiry.
    if len(order.legs) == 2:
        actions = sorted(leg.action for leg in order.legs)
        return actions == ["BUY", "SELL"]
    # Single-leg combo collapses to single-leg hedge logic.
    if len(order.legs) == 1:
        return order.legs[0].action == "BUY"
    return False


def _max_loss_usd(order: OrderLike, *, net_price: Optional[Decimal] = None) -> float:
    """Compute defined-risk max loss in USD, or `math.inf` when unbounded.

    Single-leg long option: premium × contracts × multiplier.
    Combo BUY (debit): net_debit × contracts × multiplier.
    Combo SELL (credit, defined-risk): (max_width − net_credit) × contracts × multiplier.
    Anything not classified as defined-risk → inf.
    """
    if isinstance(order, PreflightRequest):
        return _max_loss_single(order)
    if isinstance(order, ComboPreflightRequest):
        return _max_loss_combo(order, net_price=net_price)
    return math.inf


def _max_loss_single(order: PreflightRequest) -> float:
    # Stock and short options are not defined-risk for this gate's
    # purposes — naked-short shells out to Gate 4; long stock is bounded
    # by underlying price but the gate doesn't reason about it.
    if order.security_type != "OPT":
        return math.inf
    if order.action != "BUY":
        return math.inf
    return float(order.limit_price) * order.quantity * order.multiplier


def _max_loss_combo(order: ComboPreflightRequest, *, net_price: Optional[Decimal]) -> float:
    if net_price is None:
        return math.inf
    if order.action == "BUY":
        # Debit — max loss = net_debit × contracts × multiplier
        if net_price <= 0:
            return math.inf
        return float(net_price) * order.quantity * order.multiplier
    # SELL combo: credit — width − net_credit per contract
    width = _combo_max_width(order)
    if width is None:
        return math.inf
    if net_price >= 0:
        # Sign convention drift — SELL combo with non-negative
        # net_price is suspect; refuse to compute.
        return math.inf
    net_credit = abs(float(net_price))
    return max(0.0, width - net_credit) * order.quantity * order.multiplier


def _combo_max_width(order: ComboPreflightRequest) -> Optional[float]:
    """Largest strike-spread among legs of the same right.

    For a vertical, this is the leg-strike difference. For an iron
    condor (two pairs, calls + puts), it's the larger of the two pair
    widths. Returns None when widths can't be derived (mixed expiries
    treated as a non-defined-risk fallback).
    """
    expiries = {leg.expiry for leg in order.legs}
    if len(expiries) != 1:
        return None
    by_right: dict[str, list[Decimal]] = {"C": [], "P": []}
    for leg in order.legs:
        if leg.strike is None:
            return None
        by_right[leg.right].append(leg.strike)
    widths: list[float] = []
    for strikes in by_right.values():
        if len(strikes) >= 2:
            widths.append(float(max(strikes) - min(strikes)))
    return max(widths) if widths else None


# ---- Bankroll resolver --------------------------------------------------

# Test override env var per spec §4.5.1. When AccountScope grows a
# net_liq_usd attribute, that's the preferred source — for now the order
# route falls back to a documented default.
_BANKROLL_OVERRIDE_ENV = "XENON_REGIME_BANKROLL_USD_OVERRIDE"
_BANKROLL_DEFAULT_USD = 100_000.0


def resolve_bankroll_usd() -> float:
    """Bankroll source for the gate's throttle-cap math.

    Precedence:
    1. XENON_REGIME_BANKROLL_USD_OVERRIDE env var (test/dev override)
    2. Default $100,000 (TODO: replace with AccountScope.net_liq_usd or
       latest account_snapshots NAV when that integration lands)
    """
    raw = os.environ.get(_BANKROLL_OVERRIDE_ENV)
    if raw is not None and raw.strip():
        try:
            return float(raw)
        except ValueError:
            pass
    return _BANKROLL_DEFAULT_USD


# ---- Order-route integration --------------------------------------------


@dataclass(frozen=True)
class OrderGateOutcome:
    """Result of evaluating the regime gate against a candidate order.

    Disjoint shape:
    - `decision == OK / THROTTLE`: proceed with `cover_ratio` plumbed
      into preflight; if THROTTLE, also compare order's max_loss_usd
      against `max_loss_cap_usd` and 422 if exceeded.
    - `decision == BLOCK`: caller must return 409 unless an override
      with valid reason was supplied.
    """

    decision: GateDecision
    reason: str
    bind: str
    cover_ratio: float
    max_loss_cap_usd: Optional[float]
    max_loss_usd: float  # the order's computed max loss (inf when unbounded)
    state: RegimeState

    @property
    def exceeds_throttle_cap(self) -> bool:
        return (
            self.decision is GateDecision.THROTTLE
            and self.max_loss_cap_usd is not None
            and self.max_loss_usd > self.max_loss_cap_usd
        )


def evaluate_order_gate(
    order: OrderLike,
    state: RegimeState,
    *,
    bankroll_usd: Optional[float] = None,
    net_price: Optional[Decimal] = None,
) -> OrderGateOutcome:
    """Top-level helper used by the order route.

    Resolves bankroll (via `resolve_bankroll_usd` when None), runs the
    gate, computes the order's max_loss_usd, and packages everything the
    order route needs in one immutable result.
    """
    bankroll = bankroll_usd if bankroll_usd is not None else resolve_bankroll_usd()
    gate = RegimeGate.veto(order, state, bankroll, net_price=net_price)
    cover_ratio = gate.cover_ratio if gate.cover_ratio is not None else 1.0
    max_loss = _max_loss_usd(order, net_price=net_price)
    return OrderGateOutcome(
        decision=gate.decision,
        reason=gate.reason,
        bind=gate.bind,
        cover_ratio=cover_ratio,
        max_loss_cap_usd=gate.max_loss_cap_usd,
        max_loss_usd=max_loss,
        state=state,
    )
