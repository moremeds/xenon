"""Backfill uw_api_stats from history JSON."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import uw_api_stats


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def test_backfill_loads_buckets(engine, tmp_path):
    history = {
        "updated_at": "2026-04-26T09:59:20Z",
        "schema_version": 1,
        "buckets": {
            "2026-04-21T19:00:00Z": {
                "requests_2xx": 81,
                "requests_4xx": 0,
                "requests_5xx": 0,
                "cached": 0,
                "sum_latency_ms": 21525.21,
                "latency_count": 81,
            },
            "2026-04-22T14:00:00Z": {
                "requests_2xx": 1511,
                "requests_4xx": 983,
                "requests_5xx": 0,
                "cached": 296,
                "sum_latency_ms": 436723.98,
                "latency_count": 1511,
            },
        },
    }
    src = tmp_path / "uw_api_stats_history.json"
    src.write_text(json.dumps(history))

    from scripts.migrations import _2026_04_26_backfill_uw_api_stats as backfill

    backfill.run(json_path=src, db_url=_sync_test_db_url())

    with engine.begin() as conn:
        rows = conn.execute(select(uw_api_stats).order_by(uw_api_stats.c.bucket_hour)).all()
    assert len(rows) == 2
    assert rows[0].bucket_hour == datetime(2026, 4, 21, 19, 0, tzinfo=timezone.utc)
    assert rows[0].status_2xx == 81
    assert rows[0].cache_hits == 0
    assert float(rows[0].latency_sum) == 21525.21
    assert rows[1].status_4xx == 983
    assert rows[1].cache_hits == 296


def test_backfill_idempotent(engine, tmp_path):
    history = {
        "buckets": {
            "2026-04-21T19:00:00Z": {
                "requests_2xx": 50,
                "requests_4xx": 1,
                "requests_5xx": 0,
                "cached": 5,
                "sum_latency_ms": 1000.0,
                "latency_count": 50,
            },
        }
    }
    src = tmp_path / "h.json"
    src.write_text(json.dumps(history))
    from scripts.migrations import _2026_04_26_backfill_uw_api_stats as backfill

    backfill.run(json_path=src, db_url=_sync_test_db_url())
    backfill.run(json_path=src, db_url=_sync_test_db_url())  # second run
    with engine.begin() as conn:
        rows = conn.execute(select(uw_api_stats)).all()
    assert len(rows) == 1, "second run should upsert, not duplicate"
