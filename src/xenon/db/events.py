from __future__ import annotations

import asyncio
import inspect
import logging
import re
from typing import Callable

import asyncpg
from sqlalchemy import insert, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection

from xenon.db.schema import outbox

logger = logging.getLogger(__name__)

_CHANNEL_RE = re.compile(r"^[a-z_][a-z0-9_.]{0,62}$")
CHANNEL_FILL_RECORDED = "fill.recorded"
CHANNEL_FILL_COMMISSION_UPDATED = "fill.commission_updated"
CHANNEL_TRADE_CLOSED = "trade.closed"


def _validate_channel(channel: str) -> None:
    if not _CHANNEL_RE.match(channel):
        raise ValueError(f"Invalid NOTIFY channel: {channel!r}")


async def emit(
    conn: AsyncConnection,
    *,
    channel: str,
    source: str,
    payload: dict,
) -> int:
    _validate_channel(channel)
    result = await conn.execute(
        insert(outbox).values(channel=channel, source=source, payload=payload).returning(outbox.c.id)
    )
    return result.scalar()


def emit_outbox_in_txn(
    conn: Connection,
    *,
    channel: str,
    source: str,
    payload: dict,
) -> int:
    """Insert an outbox event using the caller's active transaction."""
    _validate_channel(channel)
    result = conn.execute(insert(outbox).values(channel=channel, source=source, payload=payload).returning(outbox.c.id))
    return int(result.scalar_one())


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
    """Long-lived LISTEN subscriber using a raw asyncpg connection.

    Reconnects with exponential backoff when the LISTEN connection drops.
    After every successful re-LISTEN the ``on_reconnect`` hook fires so the
    consumer can replay events missed during the gap (NOTIFY is not durable —
    messages emitted while disconnected are lost).
    """

    def __init__(
        self,
        dsn: str,
        channels: list[str],
        *,
        on_reconnect: Callable[[], object] | None = None,
        reconnect_min_delay: float = 0.5,
        reconnect_max_delay: float = 30.0,
        poll_interval: float = 5.0,
    ):
        self._dsn = dsn
        self._channels = channels
        self._conn: asyncpg.Connection | None = None
        self._callbacks: dict[str, list[Callable]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._on_reconnect = on_reconnect
        self._reconnect_min_delay = reconnect_min_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._poll_interval = poll_interval
        self._stopping = False
        self._dropped: asyncio.Event | None = None
        self._watchdog_task: asyncio.Task | None = None
        # Set when the last replay/on_reconnect attempt failed — retried with
        # backoff independently of socket health.
        self._replay_pending = False

    def on(self, channel: str, callback: Callable) -> None:
        self._callbacks.setdefault(channel, []).append(callback)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._dropped = asyncio.Event()
        self._conn = await self._open_connection()
        if self._stopping:
            # stop() raced the connect — don't leave a socket behind.
            conn = self._conn
            self._conn = None
            await conn.close()
            return
        logger.info("EventSubscriber listening on %s", self._channels)
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = self._loop.create_task(self._watchdog())

    def request_replay(self) -> None:
        """Mark replay incomplete — the watchdog retries the on_reconnect
        hook with backoff even while the LISTEN socket stays healthy."""
        self._replay_pending = True

    async def _invoke_reconnect_hook(self) -> None:
        if self._on_reconnect is None:
            return
        result = self._on_reconnect()
        if inspect.isawaitable(result):
            await result

    async def _open_connection(self) -> asyncpg.Connection:
        raw_dsn = re.sub(r"^postgresql\+\w+://", "postgresql://", self._dsn)
        conn = await asyncpg.connect(raw_dsn)
        try:
            for ch in self._channels:
                await conn.add_listener(ch, self._on_notification)
            conn.add_termination_listener(self._on_termination)
        except BaseException:
            # Setup failed or was cancelled before self._conn could own the
            # socket — close it here or it leaks.
            try:
                conn.terminate()
            except Exception:
                pass
            raise
        return conn

    def _on_termination(self, connection: asyncpg.Connection) -> None:
        # asyncpg fires this on close() too — the guards make stop() a no-op.
        if self._stopping or connection is not self._conn:
            return
        logger.warning("EventSubscriber LISTEN connection terminated; will reconnect")
        if self._dropped is not None:
            self._dropped.set()

    async def _watchdog(self) -> None:
        delay = self._reconnect_min_delay
        replay_delay = self._reconnect_min_delay
        while not self._stopping:
            conn = self._conn
            if conn is not None and not conn.is_closed():
                # Alive — sleep until the termination listener wakes us or the
                # poll interval re-checks is_closed() for drops the transport
                # has not surfaced yet.
                self._dropped.clear()
                wait = self._poll_interval
                if self._replay_pending:
                    # Replay is independent of socket health — a failed hook
                    # must retry or events missed during the outage stay lost.
                    try:
                        await self._invoke_reconnect_hook()
                    except Exception:
                        logger.exception(
                            "EventSubscriber replay retry failed; retrying in %.1fs",
                            replay_delay,
                        )
                        wait = replay_delay
                        replay_delay = min(replay_delay * 2, self._reconnect_max_delay)
                    else:
                        self._replay_pending = False
                        replay_delay = self._reconnect_min_delay
                try:
                    await asyncio.wait_for(self._dropped.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                self._conn = await self._open_connection()
            except Exception as exc:
                logger.warning(
                    "EventSubscriber reconnect failed (%s); retrying in %.1fs",
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._reconnect_max_delay)
                continue
            delay = self._reconnect_min_delay
            logger.info("EventSubscriber reconnected; listening on %s", self._channels)
            if self._on_reconnect is not None:
                try:
                    await self._invoke_reconnect_hook()
                except Exception:
                    logger.exception(
                        "EventSubscriber on_reconnect hook failed; replay marked pending"
                    )
                    self._replay_pending = True

    def _on_notification(self, connection, pid, channel, payload):
        for cb in self._callbacks.get(channel, []):
            self._loop.call_soon_threadsafe(cb, channel, payload)

    async def stop(self) -> None:
        self._stopping = True
        self._replay_pending = False
        task = self._watchdog_task
        self._watchdog_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        conn = self._conn
        self._conn = None
        if conn is not None:
            for ch in self._channels:
                try:
                    await conn.remove_listener(ch, self._on_notification)
                except Exception:
                    pass
            try:
                await conn.close()
            except Exception:
                pass
