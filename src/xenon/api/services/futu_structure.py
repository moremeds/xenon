"""Group Futu closed lots into structure-level HISTORICAL rows.

Two collapses, both keyed off the closing fill:

1. **Sub-lot aggregation.** One closing fill FIFO-matched against N open lots
   produces N ``ClosedLot`` rows for the *same* contract (e.g. a 50-lot SELL
   split across five opens → five rows). They aggregate back into one
   contract-level line.

2. **Structure fusion.** Futu places a multi-leg structure as a *single* order,
   so every leg of one structured close shares ``metadata.close_order_id``.
   Legs with the same closing order id fuse into one structure row — the
   SYMBOL column shows the underlying, the DESCRIPTION shows the structure name
   (Bull Call Spread, Long Straddle, Call Butterfly, …).

Legs closed by *different* orders stay separate single-leg rows — this is the
deliberate guard against collapsing unrelated same-underlying singles (see root
CLAUDE.md § Portfolio Structure Classification). The grouping signal is the
order id, never a same-underlying / same-expiry time heuristic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

# OCC option ticker: <UNDERLYING><YYMMDD><C|P><strike * 1000>.
_OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{4,})$")


@dataclass(frozen=True)
class OptionLeg:
    underlying: str
    expiry: str  # YYMMDD
    right: str  # 'C' | 'P'
    strike: float


def parse_occ(ticker: str) -> OptionLeg | None:
    """Parse an OCC option ticker → OptionLeg, or None for a plain stock symbol."""
    m = _OCC.match(ticker or "")
    if not m:
        return None
    underlying, ymd, right, strike_raw = m.groups()
    return OptionLeg(underlying=underlying, expiry=ymd, right=right, strike=int(strike_raw) / 1000.0)


def _contract_multiplier(ticker: str) -> int:
    return 100 if _OCC.match(ticker or "") else 1


def _fmt_strike(strike: float) -> str:
    return f"${strike:g}"


def _fmt_expiry(ymd: str) -> str:
    """YYMMDD → MM/DD/YY for the description suffix."""
    if len(ymd) == 6 and ymd.isdigit():
        return f"{ymd[2:4]}/{ymd[4:6]}/{ymd[0:2]}"
    return ymd


@dataclass
class _Leg:
    """A single contract aggregated across its FIFO sub-lots within one group."""

    ticker: str
    action: str  # closing side: 'SELL' (was long) | 'BUY' (was short)
    quantity: Decimal
    cost_basis: Decimal
    proceeds: Decimal
    realized_pnl: Decimal
    opened_at: datetime | None
    closed_at: datetime
    parsed: OptionLeg | None

    @property
    def is_long(self) -> bool:
        """Position side held before the close: SELL closed a long, BUY a short."""
        return self.action == "SELL"


def _side_word(is_long: bool) -> str:
    return "Long" if is_long else "Short"


def _right_word(right: str) -> str:
    return "Call" if right == "C" else "Put"


def classify_structure(legs: list[_Leg]) -> str:
    """Human structure name for a fused leg set. Covers the common option
    structures; falls back to ``N-Leg Structure`` for anything unrecognized."""
    opts = [leg for leg in legs if leg.parsed is not None]

    # All-stock group (no option legs).
    if not opts:
        leg = legs[0]
        return f"{_side_word(leg.is_long)} Stock"

    if len(legs) == 1:
        leg = legs[0]
        p = leg.parsed
        return f"{_side_word(leg.is_long)} {_right_word(p.right)}"

    if len(opts) == 2 and len(opts) == len(legs):
        a, b = sorted(opts, key=lambda leg: leg.parsed.strike)
        pa, pb = a.parsed, b.parsed
        same_exp = pa.expiry == pb.expiry
        same_right = pa.right == pb.right
        same_strike = pa.strike == pb.strike

        if same_exp and same_right:  # vertical
            long_leg = a if a.is_long else b
            short_leg = b if a.is_long else a
            right = _right_word(pa.right)
            if pa.right == "C":
                bull = long_leg.parsed.strike < short_leg.parsed.strike
            else:  # puts
                bull = long_leg.parsed.strike < short_leg.parsed.strike
            return f"{'Bull' if bull else 'Bear'} {right} Spread"

        if same_exp and not same_right:  # straddle / strangle / risk reversal
            if a.is_long == b.is_long:
                kind = "Straddle" if same_strike else "Strangle"
                return f"{_side_word(a.is_long)} {kind}"
            return "Risk Reversal"

        if not same_exp and same_right and same_strike:
            return "Calendar Spread"

        return "2-Leg Structure"

    if len(opts) == 3 and len(opts) == len(legs):
        rights = {leg.parsed.right for leg in opts}
        exps = {leg.parsed.expiry for leg in opts}
        strikes = {leg.parsed.strike for leg in opts}
        if len(rights) == 1 and len(exps) == 1 and len(strikes) == 3:
            return f"{_right_word(opts[0].parsed.right)} Butterfly"
        return "3-Leg Structure"

    return f"{len(legs)}-Leg Structure"


def _leg_detail(legs: list[_Leg]) -> str:
    """Compact leg descriptor appended to the structure name, e.g.
    ``06/17/26 $190/$200`` for a vertical, ``07/17/26 $220`` for a single."""
    opts = [leg for leg in legs if leg.parsed is not None]
    if not opts:
        return ""
    exps = sorted({leg.parsed.expiry for leg in opts})
    strikes = sorted({leg.parsed.strike for leg in opts})
    exp_part = "/".join(_fmt_expiry(e) for e in exps)
    strike_part = "/".join(_fmt_strike(s) for s in strikes)
    return f"{exp_part} {strike_part}".strip()


def _as_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return None


def _group_key(row: Mapping[str, Any]) -> str:
    """Closing order id when present; else the row's own close id (keeps a row
    with no order id — older data, or a fill Futu never tied to an order —
    standing alone instead of fusing with unrelated rows)."""
    meta = row.get("metadata") or {}
    oid = meta.get("close_order_id")
    if oid:
        return f"ord:{oid}"
    return f"close:{row.get('futu_close_id')}"


def _aggregate_legs(rows: list[Mapping[str, Any]]) -> list[_Leg]:
    """Collapse same-contract sub-lots within a group into one _Leg each."""
    by_ticker: dict[str, _Leg] = {}
    for row in rows:
        ticker = str(row["ticker"])
        qty = Decimal(str(row["quantity"]))
        cost = Decimal(str(row["cost_basis"]))
        proceeds = Decimal(str(row["proceeds"]))
        rpnl = Decimal(str(row["realized_pnl"]))
        opened = _as_dt(row.get("opened_at"))
        closed = _as_dt(row.get("closed_at"))
        existing = by_ticker.get(ticker)
        if existing is None:
            by_ticker[ticker] = _Leg(
                ticker=ticker,
                action=str(row["action"]),
                quantity=qty,
                cost_basis=cost,
                proceeds=proceeds,
                realized_pnl=rpnl,
                opened_at=opened,
                closed_at=closed,
                parsed=parse_occ(ticker),
            )
        else:
            existing.quantity += qty
            existing.cost_basis += cost
            existing.proceeds += proceeds
            existing.realized_pnl += rpnl
            if opened and (existing.opened_at is None or opened < existing.opened_at):
                existing.opened_at = opened
            if closed and closed > existing.closed_at:
                existing.closed_at = closed
    return list(by_ticker.values())


def _num(value: Decimal) -> float:
    return float(value)


def _leg_execution(leg: _Leg, index: int) -> dict[str, Any]:
    mult = Decimal(_contract_multiplier(leg.ticker))
    denom = leg.quantity * mult
    # Average closing price per contract: a closed long realized `proceeds`,
    # a closed short bought back at `cost_basis`.
    gross = leg.proceeds if leg.is_long else leg.cost_basis
    price = float(gross / denom) if denom else 0.0
    closed_iso = leg.closed_at.astimezone(timezone.utc).isoformat()
    return {
        "exec_id": f"{leg.ticker}-{index}",
        "time": closed_iso,
        "side": "SLD" if leg.is_long else "BOT",  # the closing trade's side
        "quantity": int(leg.quantity),
        "price": round(price, 4),
        "commission": 0,
        "notional_value": _num(gross),
        "net_cash_flow": _num(leg.realized_pnl),
    }


def build_blotter_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Group raw futu_closed_trades mappings into structure-level blotter rows.

    Output rows match the existing blotter contract (symbol, contract_desc,
    sec_type, is_closed, total_quantity, realized_pnl, cost_basis, proceeds,
    executions, …) so the frontend renders Futu identically to IB. Sorted by
    close time, most recent first.
    """
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(_group_key(row), []).append(row)

    out: list[dict[str, Any]] = []
    for grp in groups.values():
        legs = _aggregate_legs(grp)
        opts = [leg for leg in legs if leg.parsed is not None]
        underlying = opts[0].parsed.underlying if opts else legs[0].ticker
        sec_type = "OPT" if opts else "STK"

        name = classify_structure(legs)
        detail = _leg_detail(legs)
        contract_desc = f"{name} · {detail}" if detail else name

        total_qty = sum(int(leg.quantity) for leg in legs)
        realized = sum((leg.realized_pnl for leg in legs), Decimal("0"))
        cost_basis = sum((leg.cost_basis for leg in legs), Decimal("0"))
        proceeds = sum((leg.proceeds for leg in legs), Decimal("0"))
        closed_at = max(leg.closed_at for leg in legs)

        executions = [_leg_execution(leg, i) for i, leg in enumerate(legs)]
        executions.sort(key=lambda e: e["time"])

        out.append(
            {
                "symbol": underlying,
                "contract_desc": contract_desc,
                "sec_type": sec_type,
                "is_closed": True,
                "net_quantity": 0,
                "total_quantity": total_qty,
                "total_commission": 0.0,
                "realized_pnl": _num(realized),
                "cost_basis": _num(cost_basis),
                "proceeds": _num(proceeds),
                "total_cash_flow": _num(realized),
                "executions": executions,
                "perm_id": None,
                "closed_at": closed_at.astimezone(timezone.utc).isoformat(),
            }
        )

    out.sort(key=lambda r: r["closed_at"], reverse=True)
    return out


__all__ = ("OptionLeg", "parse_occ", "classify_structure", "build_blotter_rows")
