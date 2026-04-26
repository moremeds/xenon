from datetime import date, datetime, timezone
from decimal import Decimal

import pytest


@pytest.mark.asyncio
async def test_save_and_get_uw_snapshot(conn):
    from xenon.db.queries.uw import get_latest_snapshot, save_snapshot

    await save_snapshot(
        conn,
        ticker="AAPL",
        vrp_state={"iv_rank": 0.65},
        regime={"label": "high_vol"},
        flow_signals={"sweeps": 3},
        portfolio_score=Decimal("7.50"),
    )
    snap = await get_latest_snapshot(conn, ticker="AAPL")
    assert snap["ticker"] == "AAPL"
    assert snap["vrp_state"]["iv_rank"] == 0.65


@pytest.mark.asyncio
async def test_get_snapshot_history(conn):
    from xenon.db.queries.uw import get_snapshot_history, save_snapshot

    await save_snapshot(conn, ticker="AAPL", portfolio_score=Decimal("7.0"))
    await save_snapshot(conn, ticker="AAPL", portfolio_score=Decimal("8.0"))
    history = await get_snapshot_history(conn, ticker="AAPL")
    assert len(history) == 2


@pytest.mark.asyncio
async def test_save_and_get_flow_event(conn):
    from xenon.db.queries.uw import get_flow_events, save_flow_event

    await save_flow_event(
        conn,
        flow_event_key="test-evt-001",
        ticker="TSLA",
        side="call",
        strike=Decimal("250.00"),
        expiry=date(2026, 5, 16),
        detected_at=datetime.now(timezone.utc),
        initial={"premium": 50000, "oi": 1200},
        status="open",
    )
    events = await get_flow_events(conn, status="open")
    assert len(events) == 1
    assert events[0]["ticker"] == "TSLA"


@pytest.mark.asyncio
async def test_upsert_api_stats(conn):
    from xenon.db.queries.uw import get_api_stats, upsert_api_stats

    hour = datetime(2026, 4, 26, 14, 0, 0, tzinfo=timezone.utc)
    await upsert_api_stats(conn, bucket_hour=hour, requests=50, cache_hits=30, status_2xx=48, status_4xx=2)
    await upsert_api_stats(conn, bucket_hour=hour, requests=75, cache_hits=45, status_2xx=72, status_4xx=3)
    stats = await get_api_stats(conn, limit=10)
    assert len(stats) == 1
    assert stats[0]["requests"] == 75
