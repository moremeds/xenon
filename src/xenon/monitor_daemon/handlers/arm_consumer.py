"""Per-event arm-consumer DLQ harness. Spec §6.6."""
from __future__ import annotations

import asyncio
import json
import logging
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
    if key not in _attempt_counter and _already_dead_lettered(engine, source_event_id, max_attempts=max_attempts):
        return True

    try:
        arm_hook.on_fill_event(engine, payload)
        _attempt_counter.pop(key, None)
        _clear_retry_record(engine, source_event_id)
        return True
    except Exception as exc:  # noqa: BLE001
        attempts = _record_failure(engine, source_event_id=source_event_id, payload=payload, error=str(exc))
        _attempt_counter[key] = attempts
        if attempts >= max_attempts:
            _attempt_counter.pop(key, None)
            return True

        logger.warning("arm_consumer: event %s attempt %d failed: %s", source_event_id, attempts, exc)
        return False


def _already_dead_lettered(engine, source_event_id: int, *, max_attempts: int) -> bool:
    with engine.connect() as conn:
        attempts = conn.execute(
            text(
                """
                SELECT attempts FROM events.outbox_dlq
                WHERE source_event_id = :source_event_id
                  AND channel = :channel
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {"source_event_id": source_event_id, "channel": CHANNEL_FILL_RECORDED},
        ).scalar()
    return attempts is not None and int(attempts) >= max_attempts


def _record_failure(engine, *, source_event_id: int, payload: dict[str, Any], error: str) -> int:
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": source_event_id})
        row = conn.execute(
            text(
                """
                SELECT id, attempts FROM events.outbox_dlq
                WHERE source_event_id = :source_event_id
                  AND channel = :channel
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
                """
            ),
            {"source_event_id": source_event_id, "channel": CHANNEL_FILL_RECORDED},
        ).first()
        if row is None:
            attempts = 1
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
                    "error": error,
                    "attempts": attempts,
                },
            )
        else:
            attempts = int(row.attempts) + 1
            conn.execute(
                text(
                    """
                    UPDATE events.outbox_dlq
                    SET error = :error,
                        attempts = :attempts
                    WHERE id = :id
                    """
                ),
                {"id": row.id, "error": error, "attempts": attempts},
            )
        return attempts


def _clear_retry_record(engine, source_event_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                DELETE FROM events.outbox_dlq
                WHERE source_event_id = :source_event_id
                  AND channel = :channel
                """
            ),
            {"source_event_id": source_event_id, "channel": CHANNEL_FILL_RECORDED},
        )


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
