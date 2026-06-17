"""Shared FIFO lot-matcher for Futu closed trades.

Single source of FIFO truth: both the NAV backward-walk (daily realized P&L) and
the closed-trades surface (30-day HISTORICAL TRADES table + FUTU_AUTO_IMPORT
journal rows) derive from `match_closed_lots`, so the two can never drift.

Operates on `futu_trades` rows (chronological). The original Futu side lives in
`raw["trd_side"]` (BUY/SELL/SELL_SHORT/BUY_BACK); the normalized `action` column
collapses opens/closes, which would mis-match longs vs shorts — so we read raw.
Options carry a 100x contract multiplier (OCC-format ticker).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

logger = logging.getLogger(__name__)

# OCC option symbol tail: <YYMMDD><C|P><strike*1000>.
_OCC_TAIL = re.compile(r"\d{6}[CP]\d+$")


def _contract_multiplier(ticker: str) -> int:
    """100x for OCC-format option tickers; 1x for stock tickers."""
    return 100 if _OCC_TAIL.search(ticker) else 1


def _raw_trd_side(trade: dict) -> str:
    """Original Futu side from raw JSONB; falls back to the normalized action."""
    raw = trade.get("raw") or {}
    return raw.get("trd_side") or trade.get("action")


@dataclass(frozen=True)
class ClosedLot:
    futu_close_id: str
    ticker: str
    futu_code: str
    action: str  # closing side: 'SELL' (closed a long) | 'BUY' (closed a short)
    quantity: Decimal
    cost_basis: Decimal
    proceeds: Decimal
    realized_pnl: Decimal
    opened_at: datetime | None
    closed_at: datetime
    # Order id of the closing fill. Futu places a multi-leg structure as ONE
    # order, so all legs of a structure share this id — the grouping key that
    # fuses leg rows into a single structure row on the HISTORICAL surface.
    close_order_id: str = ""


def match_closed_lots(trades: list[dict]) -> list[ClosedLot]:
    """FIFO-match closing fills against open lots; emit one ClosedLot per match.

    Deterministic ordering by (filled_at, futu_deal_id) so split-lot close ids are
    stable across re-pulls (list_trades orders only by filled_at; equal timestamps
    could otherwise reorder and remint ids → duplicate journal rows).

    `futu_close_id = close_deal_id:open_deal_id` — unique even when one close spans
    multiple open lots, and stable (not a positional index).

    Closes against an empty book (pre-inception position) warn and emit nothing;
    unknown sides warn and are skipped — preserving the NAV walk's observability.
    """
    trades = sorted(trades, key=lambda t: (t["filled_at"], str(t["futu_deal_id"])))
    longs: dict[str, deque] = defaultdict(deque)  # (qty, price, opened_at, open_deal_id)
    shorts: dict[str, deque] = defaultdict(deque)
    out: list[ClosedLot] = []

    for t in trades:
        code, ticker = t["futu_code"], t["ticker"]
        mult = Decimal(_contract_multiplier(ticker))
        qty = Decimal(str(t["quantity"]))
        price = Decimal(str(t["price"]))
        when = t["filled_at"].astimezone(timezone.utc)
        deal_id = str(t["futu_deal_id"])
        close_order_id = str(t.get("futu_order_id") or "")
        side = _raw_trd_side(t)

        if side == "BUY":
            longs[code].append((qty, price, when, deal_id))
        elif side == "SELL_SHORT":
            shorts[code].append((qty, price, when, deal_id))
        elif side in ("SELL", "BUY_BACK"):
            book = longs[code] if side == "SELL" else shorts[code]
            remaining = qty
            while remaining > 0 and book:
                lot_qty, lot_price, lot_when, open_deal_id = book[0]
                matched = min(lot_qty, remaining)
                if side == "SELL":  # close a long
                    cost_basis = lot_price * matched * mult
                    proceeds = price * matched * mult
                    action = "SELL"
                else:  # BUY_BACK closes a short
                    proceeds = lot_price * matched * mult
                    cost_basis = price * matched * mult
                    action = "BUY"
                out.append(
                    ClosedLot(
                        futu_close_id=f"{deal_id}:{open_deal_id}",
                        ticker=ticker,
                        futu_code=code,
                        action=action,
                        quantity=matched,
                        cost_basis=cost_basis,
                        proceeds=proceeds,
                        realized_pnl=proceeds - cost_basis,
                        opened_at=lot_when,
                        closed_at=when,
                        close_order_id=close_order_id,
                    )
                )
                if matched == lot_qty:
                    book.popleft()
                else:
                    book[0] = (lot_qty - matched, lot_price, lot_when, open_deal_id)
                remaining -= matched
            if remaining > 0:
                logger.warning(
                    "close with no open lot: side=%s code=%s qty_unmatched=%s deal=%s (pre-inception?)",
                    side,
                    code,
                    remaining,
                    deal_id,
                )
        else:
            logger.warning("unknown trd_side=%r deal=%s — skipping", side, deal_id)

    return out


def closed_lots_to_rows(lots: list[ClosedLot]) -> list[dict]:
    """Shape ClosedLot records for xenon.db.queries.futu_history.insert_closed_trades."""
    return [
        {
            "futu_close_id": l.futu_close_id,
            "ticker": l.ticker,
            "futu_code": l.futu_code,
            "structure": None,
            "action": l.action,
            "quantity": l.quantity,
            "entry_cost": l.cost_basis,
            "exit_cost": l.proceeds,
            "realized_pnl": l.realized_pnl,
            "cost_basis": l.cost_basis,
            "proceeds": l.proceeds,
            "opened_at": l.opened_at,
            "closed_at": l.closed_at,
            "metadata": {"close_order_id": l.close_order_id} if l.close_order_id else {},
        }
        for l in lots
    ]
