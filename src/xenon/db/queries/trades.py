from __future__ import annotations

from decimal import Decimal

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import trades


async def append_trade(
    conn: AsyncConnection,
    *,
    ticker: str,
    action: str,
    quantity: int,
    structure: str | None = None,
    entry_cost: Decimal | None = None,
    exit_cost: Decimal | None = None,
    realized_pnl: Decimal | None = None,
    edge: str | None = None,
    decision: str | None = None,
    opened_at=None,
    closed_at=None,
    metadata: dict | None = None,
    broker: str = "IB",
    account_env: str = "legacy_unknown",
    broker_account: str = "legacy_unknown",
) -> int:
    result = await conn.execute(
        insert(trades)
        .values(
            ticker=ticker,
            action=action,
            quantity=quantity,
            structure=structure,
            entry_cost=entry_cost,
            exit_cost=exit_cost,
            realized_pnl=realized_pnl,
            edge=edge,
            decision=decision,
            opened_at=opened_at,
            closed_at=closed_at,
            metadata=metadata,
            broker=broker,
            account_env=account_env,
            broker_account=broker_account,
        )
        .returning(trades.c.id)
    )
    return result.scalar()


async def get_journal(
    conn: AsyncConnection,
    *,
    ticker: str | None = None,
    broker: str | None = None,
    account_env: str | None = None,
    broker_account: str | None = None,
) -> list[dict]:
    stmt = select(trades).order_by(trades.c.id)
    if ticker:
        stmt = stmt.where(trades.c.ticker == ticker)
    if broker is not None:
        stmt = stmt.where(trades.c.broker == broker)
    if account_env is not None:
        stmt = stmt.where(trades.c.account_env == account_env)
    if broker_account is not None:
        stmt = stmt.where(trades.c.broker_account == broker_account)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]
