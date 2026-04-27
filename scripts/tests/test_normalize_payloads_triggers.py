"""Regression tests for Codex-found bugs in the normalize_payloads design.

Each test pins down a specific class of bug Codex flagged:
1. gex_snapshots level columns were casting JSONB objects to numeric.
2. uw_flow_event_ticks trigger was guarding on object form, ignoring real arrays.
3. Migration replay `SET id = id` would not fire `AFTER UPDATE OF <col>` triggers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, insert, select, text

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import (
    gex_snapshots,
    uw_analyze_flow_alerts,
    uw_analyze_snapshots,
    uw_flow_event_ticks,
    uw_flow_events,
)


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def test_gex_snapshots_real_payload_shape(engine):
    """Insert the real data/gex.json payload and verify level_*_strike extracts.

    Regression: previous expression `((levels)->>'max_magnet')::numeric` cast a
    JSON object to numeric and would have failed on real data.
    """
    payload = json.loads(Path("data/gex.json").read_text())
    with engine.begin() as conn:
        conn.execute(
            insert(gex_snapshots).values(
                ticker=payload["ticker"],
                payload=payload,
            )
        )
        row = conn.execute(select(gex_snapshots)).first()
    assert float(row.level_max_magnet_strike) == 7200.0
    assert float(row.level_put_wall_strike) == 7000.0
    assert float(row.level_call_wall_strike) == 7000.0


def test_daily_track_array_fans_out(engine):
    """uw_flow_events.daily_track is an ARRAY in the production writer.

    Regression: original trigger guarded with jsonb_typeof = 'object' and
    silently skipped every real row.
    """
    daily_track = [
        {
            "date": "2026-04-22",
            "oi": 100,
            "mid": 1.20,
            "underlying_price": 184.5,
            "pct_change_premium": 0.0,
            "volume": 50,
        },
        {
            "date": "2026-04-23",
            "oi": 110,
            "mid": 1.35,
            "underlying_price": 185.2,
            "pct_change_premium": 0.125,
            "volume": 75,
        },
    ]
    with engine.begin() as conn:
        result = conn.execute(
            insert(uw_flow_events)
            .values(
                flow_event_key="TEST_AAPL_240500C_2026-05-17",
                ticker="AAPL",
                side="C",
                strike=240.0,
                detected_at="2026-04-22T15:00:00+00:00",
                initial={
                    "premium_usd": 6000.0,
                    "oi": 100,
                    "volume": 50,
                    "mid": 1.20,
                    "underlying_price": 184.5,
                },
                daily_track=daily_track,
                status="open",
            )
            .returning(uw_flow_events.c.id)
        )
        event_id = result.scalar_one()
        ticks = conn.execute(
            select(uw_flow_event_ticks)
            .where(uw_flow_event_ticks.c.event_id == event_id)
            .order_by(uw_flow_event_ticks.c.observed_at)
        ).all()
    assert len(ticks) == 2  # not 0 — trigger fired on array form
    assert ticks[0].track_date.isoformat() == "2026-04-22"
    assert ticks[0].oi == 100
    assert float(ticks[1].pct_change_premium) == 0.125


def test_migration_replay_pattern_fires_triggers(engine):
    """`UPDATE … SET <watched_col> = <watched_col>` MUST fire AFTER UPDATE OF
    <watched_col> triggers, while `SET id = id` does NOT.
    """
    flow_alerts = [{"type": "sweep", "severity": "high", "ticker": "AAPL"}]
    with engine.begin() as conn:
        result = conn.execute(
            insert(uw_analyze_snapshots)
            .values(ticker="AAPL", flow_alerts=flow_alerts)
            .returning(uw_analyze_snapshots.c.id)
        )
        sid = result.scalar_one()
        # Wipe child rows the insert trigger created
        conn.execute(
            text("DELETE FROM xenon.uw_analyze_flow_alerts WHERE snapshot_id = :sid"),
            {"sid": sid},
        )
        # Replay using the WRONG pattern — should NOT repopulate
        conn.execute(text("UPDATE xenon.uw_analyze_snapshots SET id = id WHERE id = :sid"), {"sid": sid})
        bad = conn.execute(
            text("SELECT COUNT(*) FROM xenon.uw_analyze_flow_alerts WHERE snapshot_id = :sid"),
            {"sid": sid},
        ).scalar_one()
        # Replay using the RIGHT pattern — should repopulate
        conn.execute(
            text("UPDATE xenon.uw_analyze_snapshots SET flow_alerts = flow_alerts WHERE id = :sid"),
            {"sid": sid},
        )
        good = conn.execute(
            text("SELECT COUNT(*) FROM xenon.uw_analyze_flow_alerts WHERE snapshot_id = :sid"),
            {"sid": sid},
        ).scalar_one()
    assert bad == 0
    assert good == 1
    # Sanity: the replayed child row matches the original alert
    with engine.begin() as conn:
        row = conn.execute(select(uw_analyze_flow_alerts).where(uw_analyze_flow_alerts.c.snapshot_id == sid)).first()
    assert row.alert_type == "sweep"
