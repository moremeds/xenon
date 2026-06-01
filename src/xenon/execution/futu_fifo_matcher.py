"""Deterministic FIFO lot matcher with corp-action awareness.

Inputs:
  fills:        list[FillInput]      — for one (scope, ticker)
  corp_actions: list[CorpActionInput] — same (scope, ticker)

Outputs:
  Either list[FillOutput] (default) or MatchResult(fills, lot_history) when
  return_lot_history=True. lot_history is consumed by the NAV backward walk
  to reconstruct positions after a ticker-reopen.

Stable order: events sorted by (date, kind_priority, datetime, secondary)
where fills sort BEFORE corp actions on the same date and the secondary
key is the deal_id / corp action id for tie-breaking.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, NamedTuple

log = logging.getLogger(__name__)


@dataclass
class FillInput:
    futu_deal_id: str
    action: str  # 'BUY' | 'SELL'
    quantity: int
    price: Decimal
    fees: Decimal
    filled_at: datetime


@dataclass
class CorpActionInput:
    id: str
    action_type: str  # 'split' | 'reverse_split'
    effective_date: date
    ratio_numerator: Decimal  # post / pre
    ratio_denominator: Decimal


@dataclass
class FillOutput:
    futu_deal_id: str
    action: str
    quantity: int
    price: Decimal
    fees: Decimal
    filled_at: datetime
    realized_pnl: Decimal | None


@dataclass
class _Lot:
    qty: Decimal
    avg_cost: Decimal
    qty_at_creation: Decimal
    fees_amortized: Decimal


class MatchResult(NamedTuple):
    fills: list[FillOutput]
    lot_history: list[dict]


def _event_sort_key(ev):
    kind, dt, secondary, _ = ev
    if isinstance(dt, datetime):
        key_date = dt.date()
        sub = (dt.hour, dt.minute, dt.second, dt.microsecond)
    else:
        key_date = dt
        sub = (0, 0, 0, 0)
    # Fills (kind_pri=0) sort BEFORE corp actions (kind_pri=1) on the same date.
    kind_pri = 0 if kind == "fill" else 1
    return (key_date, kind_pri, sub, str(secondary))


def match_fills(
    fills: Iterable[FillInput],
    corp_actions: Iterable[CorpActionInput],
    return_lot_history: bool = False,
):
    events = [("fill", f.filled_at, f.futu_deal_id, f) for f in fills]
    events += [("corp", c.effective_date, c.id, c) for c in corp_actions]
    events.sort(key=_event_sort_key)

    open_lots: list[_Lot] = []
    out: list[FillOutput] = []
    lot_history: list[dict] = []

    for kind, _dt, _sec, payload in events:
        if kind == "corp":
            ca = payload
            ratio = ca.ratio_numerator / ca.ratio_denominator
            for lot in open_lots:
                lot.qty *= ratio
                lot.qty_at_creation *= ratio
                lot.avg_cost /= ratio
            lot_history.append(
                {
                    "kind": "corp",
                    "date": ca.effective_date.isoformat(),
                    "ratio": float(ratio),
                }
            )
            continue

        fill: FillInput = payload
        if fill.action == "BUY":
            open_lots.append(
                _Lot(
                    qty=Decimal(fill.quantity),
                    avg_cost=fill.price,
                    qty_at_creation=Decimal(fill.quantity),
                    fees_amortized=fill.fees,
                )
            )
            out.append(
                FillOutput(
                    futu_deal_id=fill.futu_deal_id,
                    action="BUY",
                    quantity=fill.quantity,
                    price=fill.price,
                    fees=fill.fees,
                    filled_at=fill.filled_at,
                    realized_pnl=None,
                )
            )
            lot_history.append(
                {
                    "kind": "open",
                    "deal_id": fill.futu_deal_id,
                    "qty": fill.quantity,
                    "avg_cost": float(fill.price),
                    "filled_at": fill.filled_at.isoformat(),
                }
            )
        else:  # SELL
            remaining = Decimal(fill.quantity)
            gross_pnl = Decimal("0")
            matched_buy_fees = Decimal("0")
            while remaining > 0 and open_lots:
                lot = open_lots[0]
                matched_qty = min(remaining, lot.qty)
                gross_pnl += (fill.price - lot.avg_cost) * matched_qty
                matched_buy_fees += lot.fees_amortized * (matched_qty / lot.qty_at_creation)
                lot.qty -= matched_qty
                remaining -= matched_qty
                if lot.qty == 0:
                    open_lots.pop(0)
            if remaining > 0:
                log.warning(
                    "closed more than opened (deal_id=%s, remaining=%s)",
                    fill.futu_deal_id,
                    remaining,
                )
            realized = (gross_pnl - fill.fees - matched_buy_fees).quantize(Decimal("0.01"))
            out.append(
                FillOutput(
                    futu_deal_id=fill.futu_deal_id,
                    action="SELL",
                    quantity=fill.quantity,
                    price=fill.price,
                    fees=fill.fees,
                    filled_at=fill.filled_at,
                    realized_pnl=realized,
                )
            )
            lot_history.append(
                {
                    "kind": "close",
                    "deal_id": fill.futu_deal_id,
                    "qty": fill.quantity,
                    "realized_pnl": float(realized),
                    "filled_at": fill.filled_at.isoformat(),
                }
            )

    if return_lot_history:
        return MatchResult(fills=out, lot_history=lot_history)
    return out
