"""Persisted Futu history: typed insert + scope-filtered list helpers.

Source for the backward NAV walk (xenon.api.services.futu_nav_backfill).
Writes go through the M4 sync service; reads happen in M5. CLI subprocesses
build their own AsyncEngine via xenon.db.engine.get_engine().

PK semantics:
  - futu_trades: (broker, account_env, broker_account, futu_deal_id)
  - futu_cash_flow: (broker, account_env, broker_account, futu_flow_id)

`insert_*` UPSERTs (re-pulls are idempotent — Futu's deal/flow IDs are
stable, so the second run replaces fields like fees that may be recomputed
server-side after settlement).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from xenon.db.schema import futu_cash_flow, futu_trades
from xenon.execution.account_scope import AccountScope


def _scoped(row: dict, scope: AccountScope) -> dict:
    return {
        "broker": scope.broker,
        "account_env": scope.account_env,
        "broker_account": scope.broker_account,
        **row,
    }


async def insert_trades(engine: AsyncEngine, scope: AccountScope, rows: Iterable[dict]) -> int:
    rows_list = [_scoped(r, scope) for r in rows]
    if not rows_list:
        return 0
    stmt = pg_insert(futu_trades).values(rows_list)
    stmt = stmt.on_conflict_do_update(
        index_elements=["broker", "account_env", "broker_account", "futu_deal_id"],
        set_={
            "futu_order_id": stmt.excluded.futu_order_id,
            "ticker": stmt.excluded.ticker,
            "futu_code": stmt.excluded.futu_code,
            "market": stmt.excluded.market,
            "action": stmt.excluded.action,
            "quantity": stmt.excluded.quantity,
            "price": stmt.excluded.price,
            "fees": stmt.excluded.fees,
            "filled_at": stmt.excluded.filled_at,
            "raw": stmt.excluded.raw,
        },
    )
    async with engine.begin() as conn:
        result = await conn.execute(stmt)
    return result.rowcount or 0


async def insert_cashflows(engine: AsyncEngine, scope: AccountScope, rows: Iterable[dict]) -> int:
    rows_list = [_scoped(r, scope) for r in rows]
    if not rows_list:
        return 0
    stmt = pg_insert(futu_cash_flow).values(rows_list)
    stmt = stmt.on_conflict_do_update(
        index_elements=["broker", "account_env", "broker_account", "futu_flow_id"],
        set_={
            "cashflow_type": stmt.excluded.cashflow_type,
            "amount": stmt.excluded.amount,
            "currency": stmt.excluded.currency,
            "occurred_at": stmt.excluded.occurred_at,
            "raw": stmt.excluded.raw,
        },
    )
    async with engine.begin() as conn:
        result = await conn.execute(stmt)
    return result.rowcount or 0


async def list_trades(
    engine: AsyncEngine,
    scope: AccountScope,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    where = (
        (futu_trades.c.broker == scope.broker)
        & (futu_trades.c.account_env == scope.account_env)
        & (futu_trades.c.broker_account == scope.broker_account)
    )
    if since is not None:
        where = where & (futu_trades.c.filled_at >= since)
    if until is not None:
        where = where & (futu_trades.c.filled_at <= until)
    stmt = sa.select(futu_trades).where(where).order_by(futu_trades.c.filled_at.asc())
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(r) for r in rows]


async def list_cashflows(
    engine: AsyncEngine,
    scope: AccountScope,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    where = (
        (futu_cash_flow.c.broker == scope.broker)
        & (futu_cash_flow.c.account_env == scope.account_env)
        & (futu_cash_flow.c.broker_account == scope.broker_account)
    )
    if since is not None:
        where = where & (futu_cash_flow.c.occurred_at >= since)
    if until is not None:
        where = where & (futu_cash_flow.c.occurred_at <= until)
    stmt = sa.select(futu_cash_flow).where(where).order_by(futu_cash_flow.c.occurred_at.asc())
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(r) for r in rows]


__all__: Sequence[str] = (
    "insert_trades",
    "insert_cashflows",
    "list_trades",
    "list_cashflows",
)
