"""uw_flow_events.daily_track fans out to uw_flow_event_ticks; updates dedupe."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, insert, select, text

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import uw_flow_event_ticks, uw_flow_events


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def _insert_event(conn, key: str, daily_track) -> int:
    return conn.execute(
        insert(uw_flow_events)
        .values(
            flow_event_key=key,
            ticker="SPY",
            detected_at=datetime(2026, 4, 26, 14, 0, tzinfo=timezone.utc),
            initial={"premium_usd": 100000, "oi": 1000, "volume": 200, "mid": 1.10, "underlying_price": 520.0},
            daily_track=daily_track,
            status="open",
        )
        .returning(uw_flow_events.c.id)
    ).scalar()


def test_daily_track_array_initial_insert_fans_out(engine):
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
        ev_id = _insert_event(conn, "evt-001", daily_track)
        rows = conn.execute(
            select(uw_flow_event_ticks)
            .where(uw_flow_event_ticks.c.event_id == ev_id)
            .order_by(uw_flow_event_ticks.c.observed_at)
        ).all()
    assert len(rows) == 2
    assert rows[0].track_date.isoformat() == "2026-04-22"
    assert rows[0].oi == 100
    assert float(rows[1].pct_change_premium) == 0.125
    assert float(rows[1].underlying_price) == 185.2
    assert rows[0].flow_event_key == "evt-001"


def test_daily_track_array_update_adds_new_ticks_no_dupes(engine):
    daily_track = [
        {
            "date": "2026-04-22",
            "oi": 100,
            "mid": 1.20,
            "underlying_price": 184.5,
            "volume": 50,
            "pct_change_premium": 0.0,
        },
        {
            "date": "2026-04-23",
            "oi": 110,
            "mid": 1.35,
            "underlying_price": 185.2,
            "volume": 75,
            "pct_change_premium": 0.125,
        },
    ]
    with engine.begin() as conn:
        ev_id = _insert_event(conn, "evt-002", daily_track)
        new_track = list(daily_track) + [
            {
                "date": "2026-04-24",
                "oi": 120,
                "mid": 1.40,
                "underlying_price": 186.0,
                "volume": 80,
                "pct_change_premium": 0.04,
            }
        ]
        conn.execute(uw_flow_events.update().where(uw_flow_events.c.id == ev_id).values(daily_track=new_track))
        rows = conn.execute(
            select(uw_flow_event_ticks)
            .where(uw_flow_event_ticks.c.event_id == ev_id)
            .order_by(uw_flow_event_ticks.c.observed_at)
        ).all()
    assert len(rows) == 3, f"expected 3 unique ticks, got {len(rows)}"
    assert [r.track_date.isoformat() for r in rows] == ["2026-04-22", "2026-04-23", "2026-04-24"]


def test_daily_track_object_form_also_supported(engine):
    """Trigger must also handle object-shaped daily_track (legacy form)."""
    daily_track = {
        "2026-04-26T14:30:00+00:00": {
            "oi": 1000,
            "mid": 1.10,
            "underlying_price": 520.0,
            "volume": 200,
            "pct_change_premium": 0.0,
        },
        "2026-04-26T15:00:00+00:00": {
            "oi": 1050,
            "mid": 1.20,
            "underlying_price": 521.0,
            "volume": 250,
            "pct_change_premium": 0.05,
        },
    }
    with engine.begin() as conn:
        ev_id = _insert_event(conn, "evt-obj", daily_track)
        rows = conn.execute(
            select(uw_flow_event_ticks)
            .where(uw_flow_event_ticks.c.event_id == ev_id)
            .order_by(uw_flow_event_ticks.c.observed_at)
        ).all()
    assert len(rows) == 2
    assert float(rows[0].mid) == 1.10
    assert float(rows[1].mid) == 1.20


def test_null_daily_track_no_ticks(engine):
    with engine.begin() as conn:
        ev_id = _insert_event(conn, "evt-003", None)
        cnt = conn.execute(
            text("SELECT count(*) FROM xenon.uw_flow_event_ticks WHERE event_id = :i"),
            {"i": ev_id},
        ).scalar()
    assert cnt == 0
