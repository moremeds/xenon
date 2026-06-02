"""Persisted Futu history: typed insert + scope-filtered list helpers.

Source for the backward NAV walk (xenon.api.services.futu_nav_backfill).
Writes go through the M4 sync service; reads happen in M5. CLI subprocesses
build their own AsyncEngine via xenon.db.engine.get_engine().

PK semantics:
  - futu_trades: (broker, account_env, broker_account, futu_deal_id)
  - futu_cash_flow: (broker, account_env, broker_account, futu_flow_id)
  - futu_orders: (broker, account_env, broker_account, futu_order_id)
  - futu_order_fees: (broker, account_env, broker_account, futu_order_id)

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

from xenon.db.schema import futu_cash_flow, futu_order_fees, futu_orders, futu_trades
from xenon.execution.account_scope import AccountScope

# Postgres' wire protocol caps a single statement at 32767 bind params.
# Chunk per-table from the actual column count so wider tables (futu_orders,
# 22 cols) don't blow the ceiling at the same row count that fits the narrower
# ones (futu_cash_flow, 10 cols). 30000 leaves headroom for asyncpg overhead.
_BIND_BUDGET = 30000


def _batch_rows_for(table: sa.Table) -> int:
    cols = max(len(table.columns), 1)
    return max(1, _BIND_BUDGET // cols)


def _scoped(row: dict, scope: AccountScope) -> dict:
    return {
        "broker": scope.broker,
        "account_env": scope.account_env,
        "broker_account": scope.broker_account,
        **row,
    }


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


async def insert_trades(engine: AsyncEngine, scope: AccountScope, rows: Iterable[dict]) -> int:
    rows_list = [_scoped(r, scope) for r in rows]
    if not rows_list:
        return 0
    total = 0
    async with engine.begin() as conn:
        for batch in _chunks(rows_list, _batch_rows_for(futu_trades)):
            stmt = pg_insert(futu_trades).values(batch)
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
            result = await conn.execute(stmt)
            total += result.rowcount or 0
    return total


async def insert_cashflows(engine: AsyncEngine, scope: AccountScope, rows: Iterable[dict]) -> int:
    rows_list = [_scoped(r, scope) for r in rows]
    if not rows_list:
        return 0
    total = 0
    async with engine.begin() as conn:
        for batch in _chunks(rows_list, _batch_rows_for(futu_cash_flow)):
            stmt = pg_insert(futu_cash_flow).values(batch)
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
            result = await conn.execute(stmt)
            total += result.rowcount or 0
    return total


async def insert_orders(engine: AsyncEngine, scope: AccountScope, rows: Iterable[dict]) -> int:
    rows_list = [_scoped(r, scope) for r in rows]
    if not rows_list:
        return 0
    total = 0
    async with engine.begin() as conn:
        for batch in _chunks(rows_list, _batch_rows_for(futu_orders)):
            stmt = pg_insert(futu_orders).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=["broker", "account_env", "broker_account", "futu_order_id"],
                set_={
                    "ticker": stmt.excluded.ticker,
                    "futu_code": stmt.excluded.futu_code,
                    "market": stmt.excluded.market,
                    "trd_side": stmt.excluded.trd_side,
                    "order_type": stmt.excluded.order_type,
                    "order_status": stmt.excluded.order_status,
                    "quantity": stmt.excluded.quantity,
                    "price": stmt.excluded.price,
                    "dealt_quantity": stmt.excluded.dealt_quantity,
                    "dealt_avg_price": stmt.excluded.dealt_avg_price,
                    "currency": stmt.excluded.currency,
                    "created_at": stmt.excluded.created_at,
                    "updated_at": stmt.excluded.updated_at,
                    "source": stmt.excluded.source,
                    "is_combo": stmt.excluded.is_combo,
                    "leg_count": stmt.excluded.leg_count,
                    "raw": stmt.excluded.raw,
                },
            )
            result = await conn.execute(stmt)
            total += result.rowcount or 0
    return total


async def insert_order_fees(engine: AsyncEngine, scope: AccountScope, rows: Iterable[dict]) -> int:
    rows_list = [_scoped(r, scope) for r in rows]
    if not rows_list:
        return 0
    total = 0
    async with engine.begin() as conn:
        for batch in _chunks(rows_list, _batch_rows_for(futu_order_fees)):
            stmt = pg_insert(futu_order_fees).values(batch)
            stmt = stmt.on_conflict_do_update(
                index_elements=["broker", "account_env", "broker_account", "futu_order_id"],
                set_={
                    "fee_amount": stmt.excluded.fee_amount,
                    "currency": stmt.excluded.currency,
                    "raw": stmt.excluded.raw,
                },
            )
            result = await conn.execute(stmt)
            total += result.rowcount or 0
    return total


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


async def list_orders(
    engine: AsyncEngine,
    scope: AccountScope,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[dict]:
    where = (
        (futu_orders.c.broker == scope.broker)
        & (futu_orders.c.account_env == scope.account_env)
        & (futu_orders.c.broker_account == scope.broker_account)
    )
    if since is not None:
        where = where & (futu_orders.c.created_at >= since)
    if until is not None:
        where = where & (futu_orders.c.created_at <= until)
    stmt = sa.select(futu_orders).where(where).order_by(futu_orders.c.created_at.asc())
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(r) for r in rows]


async def list_order_fees(
    engine: AsyncEngine,
    scope: AccountScope,
) -> list[dict]:
    where = (
        (futu_order_fees.c.broker == scope.broker)
        & (futu_order_fees.c.account_env == scope.account_env)
        & (futu_order_fees.c.broker_account == scope.broker_account)
    )
    stmt = sa.select(futu_order_fees).where(where).order_by(futu_order_fees.c.futu_order_id.asc())
    async with engine.begin() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(r) for r in rows]


__all__: Sequence[str] = (
    "insert_trades",
    "insert_cashflows",
    "insert_orders",
    "insert_order_fees",
    "list_trades",
    "list_cashflows",
    "list_orders",
    "list_order_fees",
)
