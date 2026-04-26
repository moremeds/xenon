from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import account_snapshots, nav_history, positions


async def save_positions(conn: AsyncConnection, rows: list[dict], *, account: str) -> None:
    await conn.execute(delete(positions).where(positions.c.account == account))
    for row in rows:
        await conn.execute(insert(positions).values(**row))


async def get_positions(conn: AsyncConnection, *, account: str | None = None) -> list[dict]:
    stmt = select(positions)
    if account:
        stmt = stmt.where(positions.c.account == account)
    stmt = stmt.order_by(positions.c.ticker)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]


async def save_account_snapshot(
    conn: AsyncConnection,
    *,
    account: str,
    bankroll: Decimal,
    peak_value: Decimal | None = None,
    net_liquidation: Decimal | None = None,
) -> None:
    await conn.execute(
        insert(account_snapshots).values(
            account=account,
            bankroll=bankroll,
            peak_value=peak_value,
            net_liquidation=net_liquidation,
        )
    )


async def get_latest_snapshot(conn: AsyncConnection, *, account: str) -> dict | None:
    stmt = (
        select(account_snapshots)
        .where(account_snapshots.c.account == account)
        .order_by(account_snapshots.c.snapshot_at.desc())
        .limit(1)
    )
    result = await conn.execute(stmt)
    row = result.first()
    return dict(row._mapping) if row else None


async def upsert_nav(
    conn: AsyncConnection,
    day: date,
    *,
    nav: Decimal,
    daily_pnl: Decimal | None = None,
) -> None:
    stmt = pg_insert(nav_history).values(date=day, nav=nav, daily_pnl=daily_pnl)
    stmt = stmt.on_conflict_do_update(
        index_elements=[nav_history.c.date],
        set_={"nav": stmt.excluded.nav, "daily_pnl": stmt.excluded.daily_pnl},
    )
    await conn.execute(stmt)


async def get_nav_history(conn: AsyncConnection) -> list[dict]:
    stmt = select(nav_history).order_by(nav_history.c.date)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]
