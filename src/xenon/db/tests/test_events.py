import pytest
from sqlalchemy import select

from xenon.db.schema import outbox


@pytest.mark.asyncio
async def test_emit_inserts_into_outbox(conn):
    from xenon.db.events import emit

    await emit(conn, channel="position.synced", source="xenon", payload={"account": "IB", "count": 5})
    result = await conn.execute(select(outbox))
    rows = result.fetchall()
    assert len(rows) == 1
    assert rows[0].channel == "position.synced"
    assert rows[0].payload["count"] == 5


@pytest.mark.asyncio
async def test_get_events_since(conn):
    from xenon.db.events import emit, get_events_since

    await emit(conn, channel="scan.completed", source="xenon", payload={"type": "gex"})
    await emit(conn, channel="scan.completed", source="xenon", payload={"type": "vcg"})
    events = await get_events_since(conn, channel="scan.completed", since_id=0)
    assert len(events) == 2
    events_after_first = await get_events_since(conn, channel="scan.completed", since_id=events[0]["id"])
    assert len(events_after_first) == 1
