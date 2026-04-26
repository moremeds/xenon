import pytest


@pytest.mark.asyncio
async def test_create_and_get_session(conn):
    from xenon.db.queries.wizard import create_session, get_session

    await create_session(
        conn, session_id="ws-001", ticker="AAPL", state="planned", structure_name="vertical", intent="OPEN"
    )
    sess = await get_session(conn, "ws-001")
    assert sess["ticker"] == "AAPL"
    assert sess["state"] == "planned"


@pytest.mark.asyncio
async def test_update_session_state(conn):
    from xenon.db.queries.wizard import create_session, get_session, update_session_state

    await create_session(
        conn, session_id="ws-001", ticker="AAPL", state="planned", structure_name="vertical", intent="OPEN"
    )
    await update_session_state(conn, session_id="ws-001", state="pricing", payload={"legs": [{"strike": 150}]})
    sess = await get_session(conn, "ws-001")
    assert sess["state"] == "pricing"
    assert sess["payload"]["legs"][0]["strike"] == 150


@pytest.mark.asyncio
async def test_record_wizard_event(conn):
    from xenon.db.queries.wizard import create_session, get_events, record_event

    await create_session(
        conn, session_id="ws-001", ticker="AAPL", state="planned", structure_name="vertical", intent="OPEN"
    )
    await record_event(conn, session_id="ws-001", kind="PRICED", detail={"mid": 2.50})
    events = await get_events(conn, session_id="ws-001")
    assert len(events) == 1
    assert events[0]["kind"] == "PRICED"
