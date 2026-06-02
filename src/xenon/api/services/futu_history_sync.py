"""M4 — Futu history sync service.

Pulls orders + deals + order fees + cashflows from Futu OpenD and UPSERTs into
xenon.futu_orders + xenon.futu_trades + xenon.futu_order_fees + xenon.futu_cash_flow.
Idempotent. The M5 backward walk reads from these tables, not from Futu directly.

Design:
  - Synchronous Futu SDK lives inside FutuClient; we hold it for the duration
    of one sync, persist via the async query module, then disconnect.
  - `client_factory` is dependency-injected so tests can pass a mock without
    touching OpenD. Default is a fresh FutuClient instance.
  - Caller decides scope. The service does NOT mutate the FutuClient's
    matched_trd_env — scope persistence is the caller's responsibility
    (mirrors persist_futu_nav).
  - Non-US deals are filtered HERE (writer-side) so the M3 client stays a
    pure SDK transport. USD-only on cashflows is enforced inside M3 already.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from xenon.clients.futu_client import FutuClient
from xenon.db.queries.futu_history import insert_cashflows, insert_order_fees, insert_orders, insert_trades
from xenon.db.schema import futu_cash_flow, futu_orders, futu_trades
from xenon.execution.account_scope import AccountScope

logger = logging.getLogger(__name__)

ClientFactory = Callable[[], Any]


def _default_client_factory() -> FutuClient:
    return FutuClient()


def _trade_to_db_row(row: dict) -> dict:
    """Coerce FutuClient's dict (with floats) into M2's expected types
    (Decimal for monetary fields, JSON-safe `raw`)."""
    out = dict(row)
    out["quantity"] = Decimal(str(row["quantity"]))
    out["price"] = Decimal(str(row["price"]))
    out["fees"] = Decimal(str(row["fees"]))
    out["raw"] = _json_safe(row["raw"])
    return out


def _cashflow_to_db_row(row: dict) -> dict:
    out = dict(row)
    out["amount"] = Decimal(str(row["amount"]))
    out["raw"] = _json_safe(row["raw"])
    return out


def _order_to_db_row(row: dict) -> dict:
    out = dict(row)
    out.setdefault("source", "history_order")
    out.setdefault("is_combo", False)
    out.setdefault("leg_count", None)
    for key in ("quantity", "price", "dealt_quantity", "dealt_avg_price"):
        out[key] = Decimal(str(row[key])) if row.get(key) is not None else None
    out["raw"] = _json_safe(row["raw"])
    return out


def _fee_to_db_row(row: dict) -> dict:
    out = dict(row)
    out["fee_amount"] = Decimal(str(row["fee_amount"]))
    out["raw"] = _json_safe(row["raw"])
    return out


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_safe_value(v) for v in value]
    if isinstance(value, list):
        return [_json_safe_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe_value(v) for k, v in value.items()}
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


def _json_safe(raw: dict) -> dict:
    """Stringify datetimes + ensure JSON-serializable scalars."""
    return {str(k): _json_safe_value(v) for k, v in raw.items()}


def _derive_missing_order_rows_from_deals(deals: list[dict], existing_order_ids: set[str]) -> list[dict]:
    """Create parent order rows for deal order_ids absent from history orders.

    Futu returns multi-leg/combination parent order IDs in `history_deal_list_query`
    and `order_fee_query`, but those parent IDs are not always present in
    `history_order_list_query`. Persisting a deal-derived parent keeps fees and
    fills joinable by the real Futu `order_id` without inventing a single-leg
    ticker/price for a combo.
    """
    grouped: dict[str, list[dict]] = defaultdict(list)
    for deal in deals:
        order_id = str(deal.get("futu_order_id") or "")
        if not order_id or order_id in existing_order_ids:
            continue
        grouped[order_id].append(deal)

    rows: list[dict] = []
    for order_id in sorted(grouped):
        order_deals = sorted(grouped[order_id], key=lambda d: (d.get("filled_at"), str(d.get("futu_deal_id"))))
        tickers = sorted({str(d.get("ticker")) for d in order_deals if d.get("ticker")})
        sides = {str(d.get("action")) for d in order_deals if d.get("action")}
        filled_times = [d["filled_at"] for d in order_deals if d.get("filled_at") is not None]
        first_seen = min(filled_times) if filled_times else datetime.now(timezone.utc)
        last_seen = max(filled_times) if filled_times else first_seen
        is_combo = len(tickers) > 1 or len(order_deals) > 1
        rows.append(
            {
                "futu_order_id": order_id,
                "ticker": None,
                "futu_code": None,
                "market": "US",
                "trd_side": next(iter(sides)) if len(sides) == 1 else None,
                "order_type": "COMBO" if is_combo else "UNKNOWN",
                "order_status": "FILLED_ALL",
                "quantity": None,
                "price": None,
                "dealt_quantity": None,
                "dealt_avg_price": None,
                "currency": "USD",
                "created_at": first_seen,
                "updated_at": last_seen,
                "source": "deal_derived",
                "is_combo": is_combo,
                "leg_count": len(tickers) if tickers else len(order_deals),
                "raw": {
                    "source": "history_deal_list_query",
                    "order_id": order_id,
                    "deal_ids": [str(d.get("futu_deal_id")) for d in order_deals],
                    "tickers": tickers,
                    "sides": sorted(sides),
                    "legs": [
                        {
                            "deal_id": str(d.get("futu_deal_id")),
                            "ticker": d.get("ticker"),
                            "futu_code": d.get("futu_code"),
                            "action": d.get("action"),
                            "quantity": d.get("quantity"),
                            "price": d.get("price"),
                            "filled_at": d.get("filled_at"),
                        }
                        for d in order_deals
                    ],
                },
            }
        )
    return rows


async def resolve_incremental_since(
    engine: AsyncEngine,
    scope: AccountScope,
    inception: date,
    lookback_days: int = 7,
) -> date:
    """Return the earliest date a nightly incremental pull should fetch from.

    No persisted rows yet → return `inception` (full backfill).
    Otherwise → max(futu_orders.created_at, futu_trades.filled_at, futu_cash_flow.occurred_at) minus
    `lookback_days`. The lookback re-covers late-arriving rows (dividend tax,
    post-settlement fee corrections, retro deal updates) that Futu can post
    against a previous date.
    """
    async with engine.begin() as conn:
        scope_t = (
            (futu_trades.c.broker == scope.broker)
            & (futu_trades.c.account_env == scope.account_env)
            & (futu_trades.c.broker_account == scope.broker_account)
        )
        scope_f = (
            (futu_cash_flow.c.broker == scope.broker)
            & (futu_cash_flow.c.account_env == scope.account_env)
            & (futu_cash_flow.c.broker_account == scope.broker_account)
        )
        scope_o = (
            (futu_orders.c.broker == scope.broker)
            & (futu_orders.c.account_env == scope.account_env)
            & (futu_orders.c.broker_account == scope.broker_account)
        )
        max_order = (await conn.execute(sa.select(sa.func.max(futu_orders.c.created_at)).where(scope_o))).scalar()
        max_trade = (await conn.execute(sa.select(sa.func.max(futu_trades.c.filled_at)).where(scope_t))).scalar()
        max_flow = (await conn.execute(sa.select(sa.func.max(futu_cash_flow.c.occurred_at)).where(scope_f))).scalar()

    candidates = [d for d in (max_order, max_trade, max_flow) if d is not None]
    if not candidates:
        return inception
    watermark = max(candidates).astimezone(timezone.utc).date()
    return watermark - timedelta(days=lookback_days)


async def backfill_history_sync(
    engine: AsyncEngine,
    scope: AccountScope,
    since: datetime,
    until: Optional[datetime] = None,
    client_factory: Optional[ClientFactory] = None,
) -> dict:
    """Pull orders + deals + fees + cashflows from Futu for [since, until] and UPSERT.

    Returns a dict with counts:
      - orders_inserted        : count of rows successfully UPSERTed into futu_orders
      - order_fees_inserted    : count of rows successfully UPSERTed into futu_order_fees
      - trades_inserted        : count of rows successfully UPSERTed into futu_trades
      - cashflows_inserted     : count UPSERTed into futu_cash_flow
      - deals_filtered_non_us  : count of non-US deals dropped at the writer
      - trades_fetched         : raw count from Futu before filter
      - cashflows_fetched      : raw count from Futu (M3 already filters non-USD)

    The function disconnects the client on its way out — even on exception —
    so OpenD doesn't leak a hanging context if the caller crashes.
    """
    if until is None:
        until = datetime.now(tz=since.tzinfo)
    factory = client_factory or _default_client_factory
    client = factory()
    client.connect()
    try:
        deals_raw = client.fetch_history_deals(start=since, end=until)
        orders_raw = client.fetch_history_orders(start=since, end=until)
        us_orders = [o for o in orders_raw if o.get("market") == "US" and o.get("currency") == "USD"]
        us_deals = [d for d in deals_raw if d.get("market") == "US"]
        derived_orders = _derive_missing_order_rows_from_deals(us_deals, {str(o["futu_order_id"]) for o in us_orders})
        all_order_ids = [str(o["futu_order_id"]) for o in [*us_orders, *derived_orders]]
        order_fees_raw = client.fetch_order_fees(all_order_ids)
        cashflows_raw = client.fetch_capital_flow(start=since, end=until)
    finally:
        # Always disconnect — keep OpenD's trade context from leaking even
        # when fetch raises (rate limit, network blip, malformed row, ...).
        client.disconnect()

    n_filtered = len(deals_raw) - len(us_deals)
    if n_filtered:
        logger.info(
            "backfill_history_sync: dropped %d non-US deal(s); kept %d",
            n_filtered,
            len(us_deals),
        )

    n_orders = await insert_orders(engine, scope, [_order_to_db_row(o) for o in [*us_orders, *derived_orders]])
    n_order_fees = await insert_order_fees(engine, scope, [_fee_to_db_row(f) for f in order_fees_raw])
    n_trades = await insert_trades(engine, scope, [_trade_to_db_row(d) for d in us_deals])
    n_cashflows = await insert_cashflows(engine, scope, [_cashflow_to_db_row(f) for f in cashflows_raw])

    return {
        "orders_fetched": len(orders_raw),
        "orders_inserted": n_orders,
        "orders_derived_from_deals": len(derived_orders),
        "orders_filtered_non_usd_us": len(orders_raw) - len(us_orders),
        "order_fees_fetched": len(order_fees_raw),
        "order_fees_inserted": n_order_fees,
        "trades_fetched": len(deals_raw),
        "trades_inserted": n_trades,
        "deals_filtered_non_us": n_filtered,
        "cashflows_fetched": len(cashflows_raw),
        "cashflows_inserted": n_cashflows,
    }


__all__ = ("backfill_history_sync", "resolve_incremental_since")
