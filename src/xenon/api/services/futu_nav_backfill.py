"""M5 — Futu backward NAV walk.

Algorithm
=========
Given today's Futu net_liquidation (from accinfo_query) as the anchor,
walk backward to compute one nav_history row per calendar day:

    NAV[d-1] = NAV[d] - realized_pnl_on_d - external_cashflow_on_d

Where:
  * realized_pnl_on_d = FIFO-matched realized P&L from trades that closed
    a position on day d. Options carry a 100× contract multiplier.
  * external_cashflow_on_d = sum of Futu cashflow rows classified as
    external (per v1 rule: cashflow_type='Others' with empty remark).

Flat-line approximation: between trade days, NAV is treated as constant.
This means held-position mark-to-market changes are NOT reflected in the
walked-back curve — only realized trade P&L and external cashflows are.
Acknowledged inaccuracy; user-accepted ("same way as current Performance
page does, we can improve later").

Read-side: trades + cashflows from xenon.futu_trades / xenon.futu_cash_flow,
scope-filtered. Write-side: nav_history with source='intraday' and
daily_pnl = realized_pnl_on_d.

Idempotent via UPSERT on (broker, account_env, broker_account, date).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from xenon.db.queries.futu_history import list_cashflows, list_trades
from xenon.db.schema import nav_history
from xenon.execution.account_scope import AccountScope

logger = logging.getLogger(__name__)

# OCC option symbol: <underlier><YYMMDD><C|P><strike*1000>.
# Heuristic: tail must end with 6 digits + C/P + at least 1 digit.
_OCC_TAIL = re.compile(r"\d{6}[CP]\d+$")


def _contract_multiplier(ticker: str) -> int:
    """100x for OCC-format option tickers; 1x for stock tickers."""
    return 100 if _OCC_TAIL.search(ticker) else 1


def _is_external_cashflow(row: dict) -> bool:
    """v1 rule: cashflow_type='Others' AND empty remark = external NAV move.

    Reads cashflow_remark from the raw JSONB (M3 preserves it verbatim).
    """
    if row["cashflow_type"] != "Others":
        return False
    raw = row.get("raw") or {}
    remark = (raw.get("cashflow_remark") or "").strip()
    return remark == ""


def _raw_trd_side(trade: dict) -> str:
    """Original Futu side (BUY/SELL/SELL_SHORT/BUY_BACK) from raw JSONB.

    M3 collapses SELL_SHORT→SELL and BUY_BACK→BUY in the persisted `action`
    column for the M2/M4 surface; raw preserves the truth. M5 needs the
    truth to FIFO-match longs vs shorts correctly.
    """
    raw = trade.get("raw") or {}
    return raw.get("trd_side") or trade["action"]


def _compute_daily_realized_pnl(trades: list[dict]) -> dict[date, Decimal]:
    """FIFO-match closing trades against open lots; bucket realized P&L by day.

    Per-symbol queues track open longs and open shorts separately:
      BUY        → push to longs[code]
      SELL       → pop from longs[code]; realized = (sell_p - open_p) * qty * mult
      SELL_SHORT → push to shorts[code]
      BUY_BACK   → pop from shorts[code]; realized = (open_p - cover_p) * qty * mult

    Closes against an empty queue (account had a pre-inception position) are
    skipped with a warning — no cost basis, no P&L contribution.

    Operates on trades in chronological order; list_trades returns ascending.
    """
    longs: dict[str, deque[tuple[Decimal, Decimal]]] = defaultdict(deque)
    shorts: dict[str, deque[tuple[Decimal, Decimal]]] = defaultdict(deque)
    daily: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))

    for t in trades:
        code = t["futu_code"]
        ticker = t["ticker"]
        mult = Decimal(_contract_multiplier(ticker))
        qty = Decimal(t["quantity"])
        price = Decimal(t["price"])
        d = t["filled_at"].astimezone(timezone.utc).date()
        side = _raw_trd_side(t)

        if side == "BUY":
            longs[code].append((qty, price))
        elif side == "SELL_SHORT":
            shorts[code].append((qty, price))
        elif side == "SELL":
            remaining = qty
            while remaining > 0 and longs[code]:
                lot_qty, lot_price = longs[code][0]
                matched = min(lot_qty, remaining)
                daily[d] += (price - lot_price) * matched * mult
                if matched == lot_qty:
                    longs[code].popleft()
                else:
                    longs[code][0] = (lot_qty - matched, lot_price)
                remaining -= matched
            if remaining > 0:
                logger.warning(
                    "SELL with no open long lot: code=%s qty_unmatched=%s — skipping P&L (pre-inception position?)",
                    code,
                    remaining,
                )
        elif side == "BUY_BACK":
            remaining = qty
            while remaining > 0 and shorts[code]:
                lot_qty, lot_price = shorts[code][0]
                matched = min(lot_qty, remaining)
                daily[d] += (lot_price - price) * matched * mult
                if matched == lot_qty:
                    shorts[code].popleft()
                else:
                    shorts[code][0] = (lot_qty - matched, lot_price)
                remaining -= matched
            if remaining > 0:
                logger.warning(
                    "BUY_BACK with no open short lot: code=%s qty_unmatched=%s",
                    code,
                    remaining,
                )
        else:
            logger.warning("unknown trd_side=%r — skipping", side)

    return dict(daily)


def _sum_external_cashflows(cashflows: list[dict]) -> dict[date, Decimal]:
    daily: dict[date, Decimal] = defaultdict(lambda: Decimal("0"))
    for f in cashflows:
        if not _is_external_cashflow(f):
            continue
        d = f["occurred_at"].astimezone(timezone.utc).date()
        daily[d] += Decimal(f["amount"])
    return dict(daily)


def _earliest_activity_date(trades: list[dict], cashflows: list[dict]) -> Optional[date]:
    candidates: list[date] = []
    if trades:
        candidates.append(trades[0]["filled_at"].astimezone(timezone.utc).date())
    if cashflows:
        candidates.append(min(f["occurred_at"].astimezone(timezone.utc).date() for f in cashflows))
    return min(candidates) if candidates else None


async def _upsert_nav_history(
    engine: AsyncEngine,
    scope: AccountScope,
    rows: list[dict],
) -> int:
    if not rows:
        return 0
    stmt = pg_insert(nav_history).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["broker", "account_env", "broker_account", "date"],
        set_={
            "nav": stmt.excluded.nav,
            "daily_pnl": stmt.excluded.daily_pnl,
            "source": stmt.excluded.source,
        },
    )
    async with engine.begin() as conn:
        result = await conn.execute(stmt)
    return result.rowcount or len(rows)


async def backfill_futu_nav(
    engine: AsyncEngine,
    scope: AccountScope,
    today_nav: Decimal,
    today_date: Optional[date] = None,
    since: Optional[date] = None,
) -> int:
    """Backward-walk NAV from today_nav to `since`; UPSERT nav_history rows.

    Args:
      today_nav:  Anchor — today's Futu net_liquidation in USD.
      today_date: Defaults to today (UTC). The day `today_nav` was observed.
      since:     Defaults to earliest persisted activity (first trade or
                 first external cashflow). Walk emits rows for every
                 calendar day in [since, today_date].

    Returns the count of nav_history rows written.
    """
    if today_date is None:
        today_date = datetime.now(timezone.utc).date()

    trades = await list_trades(engine, scope)
    cashflows = await list_cashflows(engine, scope)

    pnl_by_day = _compute_daily_realized_pnl(trades)
    cashflow_by_day = _sum_external_cashflows(cashflows)

    if since is None:
        since = _earliest_activity_date(trades, cashflows) or today_date

    # Walk backward. Map every day in [since, today] to a NAV value.
    nav_by_day: dict[date, Decimal] = {}
    nav = Decimal(today_nav)
    cur = today_date
    while cur >= since:
        nav_by_day[cur] = nav
        # Step to previous day: subtract effects that landed on cur.
        nav = nav - pnl_by_day.get(cur, Decimal("0")) - cashflow_by_day.get(cur, Decimal("0"))
        cur -= timedelta(days=1)

    rows = [
        {
            "broker": scope.broker,
            "account_env": scope.account_env,
            "broker_account": scope.broker_account,
            "date": d,
            "nav": nav_by_day[d].quantize(Decimal("0.01")),
            "daily_pnl": pnl_by_day.get(d, Decimal("0")).quantize(Decimal("0.01")),
            "source": "intraday",
        }
        for d in sorted(nav_by_day)
    ]
    return await _upsert_nav_history(engine, scope, rows)


__all__ = (
    "backfill_futu_nav",
    "_compute_daily_realized_pnl",  # exported for unit-test introspection
    "_sum_external_cashflows",
    "_is_external_cashflow",
    "_contract_multiplier",
)
