"""Arm-consumer DLQ on persistent failure. Spec §6.6."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.monitor_daemon.handlers.arm_consumer import process_event_with_dlq


@pytest.fixture
def engine():
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM events.outbox_dlq WHERE source_event_id < 0"))
    yield engine
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM events.outbox_dlq WHERE source_event_id < 0"))


def test_event_moves_to_dlq_after_max_attempts(engine):
    payload = {
        "exec_id": "TEST-DLQ-1",
        "broker": "IB",
        "account_env": "paper",
        "broker_account": "DU1234567",
    }
    counter = {"n": 0}

    def always_raise(eng, p):
        counter["n"] += 1
        raise RuntimeError("boom")

    with patch("xenon.execution.brackets.arm_hook.on_fill_event", side_effect=always_raise):
        for _ in range(6):
            process_event_with_dlq(
                engine=engine,
                source_event_id=-1,
                payload=payload,
                max_attempts=5,
            )

    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM events.outbox_dlq WHERE source_event_id = -1")
        ).scalar_one()
    assert count == 1
    assert counter["n"] == 5
