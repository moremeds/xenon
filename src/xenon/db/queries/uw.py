from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import uw_analyze_snapshots, uw_api_stats, uw_flow_events


async def save_snapshot(
    conn: AsyncConnection,
    *,
    ticker: str,
    vrp_state: dict | None = None,
    regime: dict | None = None,
    flow_signals: dict | None = None,
    portfolio_score: Decimal | None = None,
) -> None:
    await conn.execute(
        insert(uw_analyze_snapshots).values(
            ticker=ticker,
            vrp_state=vrp_state,
            regime=regime,
            flow_signals=flow_signals,
            portfolio_score=portfolio_score,
        )
    )


async def get_latest_snapshot(conn: AsyncConnection, *, ticker: str) -> dict | None:
    stmt = (
        select(uw_analyze_snapshots)
        .where(uw_analyze_snapshots.c.ticker == ticker)
        .order_by(uw_analyze_snapshots.c.snapshot_at.desc(), uw_analyze_snapshots.c.id.desc())
        .limit(1)
    )
    row = (await conn.execute(stmt)).first()
    return dict(row._mapping) if row else None


async def get_snapshot_history(conn: AsyncConnection, *, ticker: str, limit: int = 100) -> list[dict]:
    stmt = (
        select(uw_analyze_snapshots)
        .where(uw_analyze_snapshots.c.ticker == ticker)
        .order_by(uw_analyze_snapshots.c.snapshot_at.desc())
        .limit(limit)
    )
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]


async def save_flow_event(
    conn: AsyncConnection,
    *,
    ticker: str,
    side: str | None = None,
    strike: Decimal | None = None,
    expiry: date | None = None,
    detected_at: datetime,
    initial: dict,
    status: str = "open",
    daily_track: dict | None = None,
    anomaly_reason: str | None = None,
    closed_at: datetime | None = None,
) -> int:
    result = await conn.execute(
        insert(uw_flow_events)
        .values(
            ticker=ticker,
            side=side,
            strike=strike,
            expiry=expiry,
            detected_at=detected_at,
            initial=initial,
            status=status,
            daily_track=daily_track,
            anomaly_reason=anomaly_reason,
            closed_at=closed_at,
        )
        .returning(uw_flow_events.c.id)
    )
    return result.scalar()


async def get_flow_events(conn: AsyncConnection, *, status: str | None = None, ticker: str | None = None) -> list[dict]:
    stmt = select(uw_flow_events).order_by(uw_flow_events.c.detected_at.desc())
    if status:
        stmt = stmt.where(uw_flow_events.c.status == status)
    if ticker:
        stmt = stmt.where(uw_flow_events.c.ticker == ticker)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]


async def upsert_api_stats(
    conn: AsyncConnection,
    *,
    bucket_hour: datetime,
    requests: int = 0,
    cache_hits: int = 0,
    latency_sum: Decimal = Decimal("0"),
    latency_count: int = 0,
    status_2xx: int = 0,
    status_4xx: int = 0,
    status_5xx: int = 0,
) -> None:
    values = dict(
        bucket_hour=bucket_hour,
        requests=requests,
        cache_hits=cache_hits,
        latency_sum=latency_sum,
        latency_count=latency_count,
        status_2xx=status_2xx,
        status_4xx=status_4xx,
        status_5xx=status_5xx,
    )
    stmt = pg_insert(uw_api_stats).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[uw_api_stats.c.bucket_hour],
        set_={k: stmt.excluded[k] for k in values if k != "bucket_hour"},
    )
    await conn.execute(stmt)


async def get_api_stats(conn: AsyncConnection, *, limit: int = 96) -> list[dict]:
    stmt = select(uw_api_stats).order_by(uw_api_stats.c.bucket_hour.desc()).limit(limit)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]
