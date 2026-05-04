"""Per-event arm-consumer DLQ harness. Spec §6.6."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any

from sqlalchemy import text

from xenon.db.events import CHANNEL_FILL_RECORDED
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
