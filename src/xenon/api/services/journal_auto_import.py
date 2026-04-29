"""Background subscriber: trade.closed → journal_entries(IB_AUTO_IMPORT).

Replaces the legacy periodic journal sync with a PG-event-driven pipeline.

NOTIFY contract: the outbox trigger emits NEW.id::text — payloads are not on
the wire. The listener fetches outbox.payload by id, upserts the journal row,
then appends its consumer id to outbox.consumed_by.

DB work runs synchronously inside `asyncio.to_thread` so it does not block
the asyncpg event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import select, update

from xenon.db.engine import get_sync_engine
from xenon.db.events import CHANNEL_TRADE_CLOSED, EventSubscriber
from xenon.db.queries.journal import AUTO_IMPORT_CONSUMER_ID, upsert_auto_import_entry
from xenon.db.schema import outbox

logger = logging.getLogger(__name__)

CONSUMER_ID = AUTO_IMPORT_CONSUMER_ID


class JournalAutoImportSubscriber:
    """Fetch-by-id listener for trade.closed outbox rows."""

    def __init__(self) -> None:
        self._subscriber: EventSubscriber | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # ---- sync core (test-friendly, single-row) ------------------------

    def handle_notification_id(self, outbox_id: int) -> None:
        engine = get_sync_engine()
        with engine.begin() as conn:
            row = conn.execute(select(outbox.c.payload, outbox.c.consumed_by).where(outbox.c.id == outbox_id)).first()
            if row is None:
                logger.warning("trade.closed id=%s not found in outbox", outbox_id)
                return
            consumed = list(row.consumed_by or [])
            if CONSUMER_ID in consumed:
                return  # already processed
            payload = row.payload or {}
            trade_id = payload.get("trade_id")
            if trade_id is None:
                logger.warning("trade.closed id=%s payload missing trade_id", outbox_id)
                return
            try:
                upsert_auto_import_entry(conn, trade_id=int(trade_id))
            except Exception:
                logger.exception("auto-import upsert failed for outbox id=%s", outbox_id)
                raise
            consumed.append(CONSUMER_ID)
            conn.execute(update(outbox).where(outbox.c.id == outbox_id).values(consumed_by=consumed))

    def replay_unconsumed(self) -> int:
        """Process every trade.closed outbox row that this consumer has not acked."""
        engine = get_sync_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                select(outbox.c.id).where(outbox.c.channel == CHANNEL_TRADE_CLOSED).order_by(outbox.c.id)
            ).fetchall()
        replayed = 0
        for row in rows:
            try:
                self.handle_notification_id(int(row.id))
                replayed += 1
            except Exception:
                logger.exception("replay failed for outbox id=%s", row.id)
        return replayed

    # ---- async wiring -------------------------------------------------

    async def start(self) -> None:
        dsn = os.environ.get("DATABASE_URL", "")
        if not dsn:
            logger.warning("DATABASE_URL not set; journal auto-import listener disabled")
            return
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self.replay_unconsumed)
        self._subscriber = EventSubscriber(dsn=dsn, channels=[CHANNEL_TRADE_CLOSED])
        self._subscriber.on(CHANNEL_TRADE_CLOSED, self._on_notification)
        await self._subscriber.start()
        logger.info("journal auto-import listener started")

    def _on_notification(self, _channel: str, payload: str) -> None:
        try:
            outbox_id = int(payload)
        except (TypeError, ValueError):
            logger.warning("trade.closed NOTIFY payload not int: %r", payload)
            return
        loop = self._loop
        if loop is None:
            logger.error("listener received notification before loop was set")
            return
        loop.create_task(asyncio.to_thread(self.handle_notification_id, outbox_id))

    async def stop(self) -> None:
        if self._subscriber is not None:
            await self._subscriber.stop()
            self._subscriber = None
