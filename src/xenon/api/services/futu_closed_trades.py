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
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal

logger = logging.getLogger(__name__)

# (futu_code, side, deal_id) of unmatched closes already warned about in this process.
_UNMATCHED_CLOSE_WARNED: set[tuple[str, str, str]] = set()

# OCC option symbol tail: <YYMMDD><C|P><strike*1000>.
_OCC_TAIL = re.compile(r"\d{6}[CP]\d+$")

# Same tail, split into expiry / right / strike-milli parts for rename re-keying.
_OCC_PARTS = re.compile(r"^(\d{6})([CP])(\d+)$")


@dataclass(frozen=True)
class CorporateAction:
    """One reviewed corporate action affecting futu_code / quantity / price.

    kind="split":  deals of `code` filled before `ex_date` get quantity×ratio and
      price÷ratio (ratio = new shares per old share — a 1:10 split is 10, a 10:1
      reverse split is 0.1). Total cost is unchanged.
    kind="rename": option deals of the `code` contract family filled before
      `ex_date` are re-keyed to `new_code` plus their OCC tail, so opens booked
      under the pre-adjustment code match closes booked under the post-adjustment
      code. A non-None `ratio` is a per-share strike adjustment in USD subtracted
      from the strike milli value (OCC special-dividend adjustment); None keeps
      the tail unchanged (contract re-code only). Plain stock codes never match —
      they carry no OCC tail — so a rename on "US.LCID" cannot touch US.LCID.
    """

    code: str
    kind: str  # "split" | "rename"
    ex_date: date
    ratio: Decimal | None = None
    new_code: str | None = None


# Reviewed static list — source: apex-lake get_corporate_actions (provider
# massive), as recorded in tmp/repo-review/evidence/futu-closed-trades-unmatched.md
# §1.3, checked 2026-10-22.
CORPORATE_ACTIONS: tuple[CorporateAction, ...] = (
    # Stock splits — ratio = new shares per old share.
    CorporateAction("US.KLAC", "split", date(2026, 6, 12), Decimal("10")),  # 1:10
    CorporateAction("US.MULL", "split", date(2026, 6, 26), Decimal("25")),  # 1:25
    CorporateAction("US.MUU", "split", date(2026, 7, 15), Decimal("20")),  # 1:20
    CorporateAction("US.RKLX", "split", date(2025, 12, 9), Decimal("3")),  # 1:3
    # LCID 10:1 reverse split ex 2025-09-02: option contracts re-coded
    # LCID… -> LCID1… (deliverable change only; per-contract qty/price unchanged).
    CorporateAction("US.LCID", "rename", date(2025, 9, 2), new_code="US.LCID1"),
    # ZIM $0.84 special dividend ex 2024-12-02: option strikes reduced by $0.84
    # (P19000 -> P18160, P20000 -> P19160).
    CorporateAction("US.ZIM", "rename", date(2024, 12, 2), Decimal("0.84"), "US.ZIM"),
)


def _futu_trade_date(filled_at: datetime) -> date:
    """US trade date of a fill — the calendar date the ex-date gate compares.

    Stored `filled_at` is Futu `create_time` — US/Eastern wall time labelled UTC
    at ingest (`FutuClient._parse_futu_ts`), verified 2026-10-22 on the 7378-row
    prod export: regular-session stock fills cluster at stored-UTC hours 09-16
    (= ET session 09:30-16:00), not 13-21 — see
    tmp/evidence/futu-corp-actions-notes.md § Timestamp convention. The stored
    UTC calendar date is therefore already the ET trade date; no tz conversion
    is applied. Overnight-session (20:00-04:00 ET) corporate-action treatment is
    unverified; no stored fill hits that boundary.
    """
    return filled_at.astimezone(timezone.utc).date()


def _renamed_code(futu_code: str, action: CorporateAction) -> str | None:
    """New futu_code for a pre-ex option deal, or None when the code is not in
    the action's contract family (incl. plain stock codes — no OCC tail)."""
    tail = futu_code.removeprefix(action.code)
    if tail == futu_code:
        return None
    m = _OCC_PARTS.match(tail)
    if not m:
        return None
    expiry, right, strike = m.groups()
    if action.ratio is not None:
        strike = f"{int(strike) - int(action.ratio * 1000):0{len(strike)}d}"
    return f"{action.new_code}{expiry}{right}{strike}"


def apply_corporate_actions(trades: list[dict], actions: Iterable[CorporateAction]) -> list[dict]:
    """Return `trades` with the given corporate actions applied. Pure: the input
    rows are never mutated (adjusted rows are new dicts); with no actions the
    input is returned unchanged.
    """
    if not actions:
        return trades
    out: list[dict] = []
    for t in trades:
        when = _futu_trade_date(t["filled_at"])
        adj = t
        for a in actions:
            if when >= a.ex_date:
                continue
            if a.kind == "split" and adj["futu_code"] == a.code:
                adj = dict(adj)
                adj["quantity"] = Decimal(str(adj["quantity"])) * a.ratio
                adj["price"] = Decimal(str(adj["price"])) / a.ratio
            elif a.kind == "rename":
                new_code = _renamed_code(str(adj["futu_code"]), a)
                if new_code is not None:
                    adj = dict(adj)
                    adj["futu_code"] = new_code
        out.append(adj)
    return out


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


def match_closed_lots(trades: list[dict], actions: Iterable[CorporateAction] = ()) -> list[ClosedLot]:
    """FIFO-match closing fills against open lots; emit one ClosedLot per match.

    Deterministic ordering by (filled_at, futu_deal_id) so split-lot close ids are
    stable across re-pulls (list_trades orders only by filled_at; equal timestamps
    could otherwise reorder and remint ids → duplicate journal rows).

    `futu_close_id = close_deal_id:open_deal_id` — unique even when one close spans
    multiple open lots, and stable (not a positional index).

    `actions` is a pure corporate-actions pre-pass (see apply_corporate_actions):
    splits re-scale pre-ex qty/price and contract renames re-key pre-ex option
    codes, so post-action closes match their pre-action opens. Both production
    callers pass CORPORATE_ACTIONS; tests may pass () for the raw matcher.

    Closes against an empty book emit nothing and warn once per process per deal
    (later rebuilds log at DEBUG); unknown sides warn and are skipped — preserving the NAV walk's observability.
    """
    trades = apply_corporate_actions(trades, actions)
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
                # The close has no open lot. Likely causes: the position was
                # carried over from the pre-upgrade account (deal history is
                # acc_id-scoped — for this account it starts at the 2024-07-15
                # "Account Upgrade" transfer, not the 2024-01-01 request window),
                # a corporate action not yet in CORPORATE_ACTIONS, or a missed
                # fill. The realized P&L of the unmatched quantity is omitted in
                # every case. The rebuild reruns on every sync, so warn once per
                # process per deal, then DEBUG.
                key = (code, side, deal_id)
                unseen = key not in _UNMATCHED_CLOSE_WARNED
                _UNMATCHED_CLOSE_WARNED.add(key)
                logger.log(
                    logging.WARNING if unseen else logging.DEBUG,
                    "close with no open lot: side=%s code=%s qty_unmatched=%s deal=%s (likely cause: pre-upgrade transfer-in or unhandled corporate action)",
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
