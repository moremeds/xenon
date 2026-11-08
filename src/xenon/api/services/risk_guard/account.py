"""Account view and ratios (spec §5.2). Read-only.

Source: the latest IB ``account_snapshots`` row (``payload`` + ``snapshot_at``).

- leverage = sum of max loss / NLV. Max loss of a position is computed from
  its legs at expiry: the worst P&L over S in {0} + strikes, given the signed
  ``entry_cost`` (debit > 0). This covers ib_sync's same-expiry "Combo (N
  legs)" rows, which carry no ``max_risk``. A position with unbounded loss
  (net short calls / stock above the top strike) or unreadable legs is
  counted in ``undefined_positions``, not summed;
- width = the same worst case without the premium (a credit vertical: strike
  width x 100 x qty); cushion = ExcessLiquidity / NLV;
- strike distance per option leg with a known underlying spot, as a fraction of
  spot, positive = out of the money (put: (S - K) / S, call: (K - S) / S);
- drawdown = NLV / peak - 1, peak = the highest ``nav_history`` close after
  adjusting for later deposits and withdrawals (``flow_adjusted_peak``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

MULTIPLIER = 100  # index / equity options; ib_sync uses the same constant for max_risk


@dataclass(frozen=True)
class LegDistance:
    ticker: str
    expiry: str | None
    right: str
    strike: float
    side: str  # short | long
    distance: float


@dataclass(frozen=True)
class AccountView:
    nlv: float | None
    excess_liquidity: float | None
    cushion: float | None
    max_loss: float
    leverage: float | None
    width: float
    width_ratio: float | None
    undefined_positions: int
    positions_without_spot: int
    distances: tuple[LegDistance, ...]
    min_short_distance: float | None
    shorts_crossed: int
    long_itm: bool
    snapshot_age_s: float


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _ratio(a: float | None, b: float | None) -> float | None:
    return a / b if a is not None and b else None


def _expiry_risk(pos: dict) -> tuple[float, float] | None:
    """(max loss incl. premium, max loss of the payoff alone) at expiry; None = unbounded or unreadable."""
    entry = _num(pos.get("entry_cost"))
    legs: list[tuple[str, float | None, float]] = []
    for leg in pos.get("legs") or []:
        qty = _num(leg.get("contracts"))
        kind = leg.get("type")
        if qty is None or kind not in ("Put", "Call", "Stock"):
            return None
        strike = _num(leg.get("strike"))
        if kind != "Stock" and strike is None:
            return None
        legs.append((kind, strike, qty if leg.get("direction") == "LONG" else -qty))
    if entry is None or not legs:
        return None
    # Slope above the top strike: calls x 100 + shares. Negative = unbounded loss.
    if sum(q * (MULTIPLIER if k == "Call" else 1) for k, _, q in legs if k != "Put") < 0:
        return None

    def payoff(spot: float) -> float:
        total = 0.0
        for kind, strike, q in legs:
            if kind == "Stock":
                total += q * spot
            elif kind == "Put":
                total += q * MULTIPLIER * max(strike - spot, 0.0)
            else:
                total += q * MULTIPLIER * max(spot - strike, 0.0)
        return total

    worst = min(payoff(x) for x in [0.0] + [k for _, k, _ in legs if k is not None])
    option_worst = min(
        sum(q * MULTIPLIER * max((k - x) if kind == "Put" else (x - k), 0.0) for kind, k, q in legs if kind != "Stock")
        for x in [0.0] + [k for _, k, _ in legs if k is not None]
    )
    return max(entry - worst, 0.0), max(-option_worst, 0.0)


def account_view(payload: dict, snapshot_at: datetime, now: datetime, spots: dict[str, float]) -> AccountView:
    summary = payload.get("account_summary") or {}
    nlv = _num(summary.get("net_liquidation"))
    excess = _num(summary.get("excess_liquidity"))
    max_loss = width = 0.0
    undefined = without_spot = 0
    distances: list[LegDistance] = []
    long_itm = False
    for pos in payload.get("positions") or []:
        risk = _expiry_risk(pos)
        if risk is None:
            undefined += 1
        else:
            max_loss += risk[0]
            width += risk[1]
        legs = [leg for leg in pos.get("legs") or [] if leg.get("type") in ("Put", "Call")]
        if not legs:
            continue
        spot = spots.get(pos.get("ticker") or "")
        if not spot:
            without_spot += 1
            continue
        net_credit = (_num(pos.get("entry_cost")) or 0.0) < 0
        for leg in legs:
            strike = _num(leg.get("strike"))
            if strike is None:
                continue
            right = "P" if leg["type"] == "Put" else "C"
            dist = (spot - strike) / spot if right == "P" else (strike - spot) / spot
            side = "short" if leg.get("direction") == "SHORT" else "long"
            distances.append(LegDistance(pos["ticker"], pos.get("expiry"), right, strike, side, dist))
            if side == "long" and dist < 0 and net_credit:
                long_itm = True
    shorts = [d.distance for d in distances if d.side == "short"]
    return AccountView(
        nlv=nlv,
        excess_liquidity=excess,
        cushion=_ratio(excess, nlv),
        max_loss=max_loss,
        leverage=_ratio(max_loss, nlv),
        width=width,
        width_ratio=_ratio(width, nlv),
        undefined_positions=undefined,
        positions_without_spot=without_spot,
        distances=tuple(distances),
        min_short_distance=min(shorts) if shorts else None,
        shorts_crossed=sum(d < 0 for d in shorts),
        long_itm=long_itm,
        snapshot_age_s=(now - snapshot_at).total_seconds(),
    )


def flow_adjusted_peak(navs: Iterable[tuple[date, float]], flows: Iterable[tuple[date, float]]) -> float | None:
    """max over close dates t of (nav_t + net flows dated after t).

    A deposit after the peak raises the bar (it is not a gain); a withdrawal
    lowers it. A flow on date t is already inside the close of t.
    """
    by_day: dict[date, float] = {}
    for d, amount in flows:
        by_day[d] = by_day.get(d, 0.0) + amount
    flow_days = sorted(by_day)
    peak: float | None = None
    later = 0.0
    i = len(flow_days) - 1
    for d, nav in sorted(navs, reverse=True):
        while i >= 0 and flow_days[i] > d:
            later += by_day[flow_days[i]]
            i -= 1
        peak = nav + later if peak is None else max(peak, nav + later)
    return peak


def drawdown(nlv: float | None, peak: float | None) -> float | None:
    return nlv / peak - 1.0 if nlv is not None and peak else None


@dataclass(frozen=True)
class DrawdownBasis:
    peak: float | None
    peak_30d: float | None
    adjusted: bool  # False = no cash-flow rows for the scope: the alert says "unadjusted"
    flows_since: date | None  # earliest flow on file; deposits before it are not adjusted


async def load_drawdown_basis(engine, scope, today: date) -> DrawdownBasis:
    """Close NAVs (``source='close'``) and IB Flex cash flows for ``scope``."""
    from xenon.api.services.performance_ib_flows import load_ib_flows_per_day
    from xenon.db.queries.nav_history import load_nav_curve

    curve = await load_nav_curve(engine, scope, date(2000, 1, 1), today)
    closes = [(r.date, float(r.nav)) for r in curve.itertuples() if r.source == "close"]
    series = await load_ib_flows_per_day(engine, scope, since=date(2000, 1, 1), until=today)
    flows = [(d, float(v)) for d, v in series.items()]
    since_30 = today - timedelta(days=30)
    return DrawdownBasis(
        peak=flow_adjusted_peak(closes, flows),
        peak_30d=flow_adjusted_peak([c for c in closes if c[0] >= since_30], flows),
        adjusted=bool(flows),
        flows_since=min((d for d, _ in flows), default=None),
    )
