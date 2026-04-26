from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import ticker_cache


async def set_cached(
    conn: AsyncConnection,
    *,
    ticker: str,
    cache_type: str,
    data: dict,
    expires_at: datetime | None = None,
) -> None:
    stmt = pg_insert(ticker_cache).values(
        ticker=ticker,
        cache_type=cache_type,
        data=data,
        expires_at=expires_at,
        updated_at=datetime.now(timezone.utc),
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["ticker", "cache_type"],
        set_={
            "data": stmt.excluded.data,
            "expires_at": stmt.excluded.expires_at,
            "updated_at": stmt.excluded.updated_at,
        },
    )
    await conn.execute(stmt)


async def get_cached(conn: AsyncConnection, *, ticker: str, cache_type: str) -> dict | None:
    now = datetime.now(timezone.utc)
    stmt = select(ticker_cache).where(
        and_(
            ticker_cache.c.ticker == ticker,
            ticker_cache.c.cache_type == cache_type,
            (ticker_cache.c.expires_at.is_(None)) | (ticker_cache.c.expires_at > now),
        )
    )
    row = (await conn.execute(stmt)).first()
    return dict(row._mapping) if row else None


async def delete_expired(conn: AsyncConnection) -> int:
    now = datetime.now(timezone.utc)
    result = await conn.execute(delete(ticker_cache).where(ticker_cache.c.expires_at <= now))
    return result.rowcount
