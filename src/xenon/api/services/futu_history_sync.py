"""M4 — Futu history sync service.

Pulls trades + cashflows from Futu OpenD (via FutuClient.fetch_history_deals
and fetch_capital_flow) and UPSERTs into xenon.futu_trades + xenon.futu_cash_flow.
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
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from xenon.clients.futu_client import FutuClient
from xenon.db.queries.futu_history import insert_cashflows, insert_trades
from xenon.db.schema import futu_cash_flow, futu_trades
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


def _json_safe(raw: dict) -> dict:
    """Stringify datetimes + ensure JSON-serializable scalars."""
    safe: dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(v, datetime):
            safe[k] = v.isoformat()
        elif hasattr(v, "isoformat"):
            safe[k] = v.isoformat()
        elif isinstance(v, (int, float, str, bool)) or v is None:
            safe[k] = v
        else:
            safe[k] = str(v)
    return safe


async def resolve_incremental_since(
    engine: AsyncEngine,
    scope: AccountScope,
    inception: date,
    lookback_days: int = 7,
) -> date:
    """Return the earliest date a nightly incremental pull should fetch from.

    No persisted rows yet → return `inception` (full backfill).
    Otherwise → max(futu_trades.filled_at, futu_cash_flow.occurred_at) minus
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
        max_trade = (await conn.execute(sa.select(sa.func.max(futu_trades.c.filled_at)).where(scope_t))).scalar()
        max_flow = (await conn.execute(sa.select(sa.func.max(futu_cash_flow.c.occurred_at)).where(scope_f))).scalar()

    candidates = [d for d in (max_trade, max_flow) if d is not None]
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
    """Pull deals + cashflows from Futu for [since, until] and UPSERT.

    Returns a dict with counts:
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
        cashflows_raw = client.fetch_capital_flow(start=since, end=until)
    finally:
        # Always disconnect — keep OpenD's trade context from leaking even
        # when fetch raises (rate limit, network blip, malformed row, ...).
        client.disconnect()

    us_deals = [d for d in deals_raw if d.get("market") == "US"]
    n_filtered = len(deals_raw) - len(us_deals)
    if n_filtered:
        logger.info(
            "backfill_history_sync: dropped %d non-US deal(s); kept %d",
            n_filtered,
            len(us_deals),
        )

    n_trades = await insert_trades(engine, scope, [_trade_to_db_row(d) for d in us_deals])
    n_cashflows = await insert_cashflows(engine, scope, [_cashflow_to_db_row(f) for f in cashflows_raw])

    return {
        "trades_fetched": len(deals_raw),
        "trades_inserted": n_trades,
        "deals_filtered_non_us": n_filtered,
        "cashflows_fetched": len(cashflows_raw),
        "cashflows_inserted": n_cashflows,
    }


__all__ = ("backfill_history_sync", "resolve_incremental_since")
