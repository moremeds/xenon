from __future__ import annotations

import asyncio
import logging
from typing import Callable

import asyncpg
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import outbox

logger = logging.getLogger(__name__)


async def emit(
    conn: AsyncConnection,
    *,
    channel: str,
    source: str,
    payload: dict,
) -> int:
    result = await conn.execute(
        insert(outbox).values(channel=channel, source=source, payload=payload).returning(outbox.c.id)
    )
    return result.scalar()


async def get_events_since(
    conn: AsyncConnection,
    *,
    channel: str,
    since_id: int,
    limit: int = 100,
) -> list[dict]:
    stmt = select(outbox).where(outbox.c.channel == channel, outbox.c.id > since_id).order_by(outbox.c.id).limit(limit)
    result = await conn.execute(stmt)
    return [dict(row._mapping) for row in result]


class EventSubscriber:
    """Long-lived LISTEN subscriber using a raw asyncpg connection."""

    def __init__(self, dsn: str, channels: list[str]):
        self._dsn = dsn
        self._channels = channels
        self._conn: asyncpg.Connection | None = None
        self._callbacks: dict[str, list[Callable]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def on(self, channel: str, callback: Callable) -> None:
        self._callbacks.setdefault(channel, []).append(callback)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        raw_dsn = self._dsn.replace("+asyncpg", "").replace("postgresql+asyncpg", "postgresql")
        self._conn = await asyncpg.connect(raw_dsn)
        for ch in self._channels:
            await self._conn.add_listener(ch, self._on_notification)
        logger.info("EventSubscriber listening on %s", self._channels)

    def _on_notification(self, connection, pid, channel, payload):
        for cb in self._callbacks.get(channel, []):
            self._loop.call_soon_threadsafe(cb, channel, payload)

    async def stop(self) -> None:
        if self._conn:
            for ch in self._channels:
                await self._conn.remove_listener(ch, self._on_notification)
            await self._conn.close()
            self._conn = None
