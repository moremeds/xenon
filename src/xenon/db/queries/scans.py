from __future__ import annotations

from decimal import Decimal

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import cri_series, scan_results


async def save_scan(conn: AsyncConnection, *, scan_type: str, payload: dict) -> None:
    await conn.execute(insert(scan_results).values(scan_type=scan_type, payload=payload))


async def get_latest_scan(conn: AsyncConnection, *, scan_type: str) -> dict | None:
    stmt = (
        select(scan_results)
        .where(scan_results.c.scan_type == scan_type)
        .order_by(scan_results.c.scanned_at.desc(), scan_results.c.id.desc())
        .limit(1)
    )
    row = (await conn.execute(stmt)).first()
    return dict(row._mapping) if row else None


async def save_cri_datapoint(
    conn: AsyncConnection,
    *,
    cri_level: Decimal,
    alert: bool = False,
    payload: dict | None = None,
) -> None:
    await conn.execute(insert(cri_series).values(cri_level=cri_level, alert=alert, payload=payload))


async def get_cri_series(conn: AsyncConnection, *, limit: int = 100) -> list[dict]:
    stmt = select(cri_series).order_by(cri_series.c.recorded_at).limit(limit)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]
