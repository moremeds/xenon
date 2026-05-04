"""Per-event arm-consumer DLQ harness. Spec §6.6."""
from __future__ import annotations

import json
import logging
import asyncio
import os
from collections import defaultdict
from typing import Any

from sqlalchemy import text

from xenon.db.events import CHANNEL_FILL_RECORDED, EventSubscriber
from xenon.execution.brackets import arm_hook

logger = logging.getLogger(__name__)

_attempt_counter: dict[tuple[str, int], int] = defaultdict(int)


def process_event_with_dlq(
    *,
    engine,
    source_event_id: int,
    payload: dict[str, Any],
    max_attempts: int = 5,
) -> bool:
    """Return True when the event is processed or dead-lettered."""
    key = (CHANNEL_FILL_RECORDED, source_event_id)
    if key not in _attempt_counter and _already_dead_lettered(engine, source_event_id):
        return True

    try:
        arm_hook.on_fill_event(engine, payload)
        _attempt_counter.pop(key, None)
        return True
    except Exception as exc:  # noqa: BLE001
        _attempt_counter[key] += 1
        attempts = _attempt_counter[key]
        if attempts >= max_attempts:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO events.outbox_dlq
                            (source_event_id, channel, source, payload, error, attempts)
                        VALUES
                            (:source_event_id, :channel, 'arm_consumer',
                             CAST(:payload AS jsonb), :error, :attempts)
                        """
                    ),
                    {
                        "source_event_id": source_event_id,
                        "channel": CHANNEL_FILL_RECORDED,
                        "payload": json.dumps(payload),
                        "error": str(exc),
                        "attempts": attempts,
                    },
                )
            _attempt_counter.pop(key, None)
            return True

        logger.warning("arm_consumer: event %s attempt %d failed: %s", source_event_id, attempts, exc)
        return False


def _already_dead_lettered(engine, source_event_id: int) -> bool:
    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM events.outbox_dlq WHERE source_event_id = :source_event_id"),
            {"source_event_id": source_event_id},
        ).scalar_one()
    return bool(count)


async def _listen_loop() -> None:
    """Long-lived LISTEN coroutine; one subscriber per daemon process."""
    from xenon.db.engine import get_sync_engine

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.warning("arm_consumer: DATABASE_URL unset; listen loop disabled")
        return

    engine = get_sync_engine()
    subscriber = EventSubscriber(dsn=dsn, channels=[CHANNEL_FILL_RECORDED])
    subscriber.on(CHANNEL_FILL_RECORDED, lambda _channel, payload: _dispatch(engine, payload))
    await subscriber.start()
    try:
        while True:
            await asyncio.sleep(60)
    finally:
        await subscriber.stop()


def _dispatch(engine, raw_payload: str | None) -> None:
    if raw_payload is None:
        return

    source_event_id: int
    payload: dict[str, Any]
    if raw_payload.isdigit():
        source_event_id = int(raw_payload)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT payload FROM events.outbox WHERE id = :event_id"),
                {"event_id": source_event_id},
            ).first()
        if row is None:
            logger.warning("arm_consumer: outbox event %s not found", source_event_id)
            return
        payload = dict(row.payload or {})
    else:
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            logger.warning("arm_consumer: malformed NOTIFY payload; skipping")
            return
        source_event_id = int(payload.get("__outbox_id", -1))

    process_event_with_dlq(engine=engine, source_event_id=source_event_id, payload=payload)


def start_listen_loop() -> None:
    """Sync entry point for MonitorDaemon's side-task thread."""
    asyncio.run(_listen_loop())
