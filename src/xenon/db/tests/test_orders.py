from decimal import Decimal

import pytest


@pytest.mark.asyncio
async def test_reserve_attempt(conn):
    from xenon.db.queries.orders import reserve_attempt

    result = await reserve_attempt(
        conn,
        submission_id="sub-001",
        user_id="user-1",
        client_attempt_id="att-1",
        ticker="AAPL",
        security_type="STK",
        action="BUY",
        quantity=100,
        limit_price=Decimal("150.00"),
    )
    assert result["submission_id"] == "sub-001"
    assert result["state"] == "PENDING"


@pytest.mark.asyncio
async def test_reserve_attempt_idempotent(conn):
    from xenon.db.queries.orders import reserve_attempt

    kwargs = dict(
        submission_id="sub-001",
        user_id="user-1",
        client_attempt_id="att-1",
        ticker="AAPL",
        security_type="STK",
        action="BUY",
        quantity=100,
        limit_price=Decimal("150"),
    )
    r1 = await reserve_attempt(conn, **kwargs)
    r2 = await reserve_attempt(conn, **kwargs)
    assert r1["submission_id"] == r2["submission_id"]


@pytest.mark.asyncio
async def test_mark_submitted(conn):
    from xenon.db.queries.orders import get_by_submission_id, mark_submitted, reserve_attempt

    await reserve_attempt(
        conn,
        submission_id="sub-001",
        user_id="user-1",
        client_attempt_id="att-1",
        ticker="AAPL",
        security_type="STK",
        action="BUY",
        quantity=100,
        limit_price=Decimal("150"),
    )
    await mark_submitted(conn, submission_id="sub-001", ib_order_id=12345, perm_id=99999, placing_client_id=1)
    row = await get_by_submission_id(conn, "sub-001")
    assert row["state"] == "WORKING"
    assert row["ib_order_id"] == "12345"


@pytest.mark.asyncio
async def test_mark_terminal(conn):
    from xenon.db.queries.orders import get_by_submission_id, mark_submitted, mark_terminal, reserve_attempt

    await reserve_attempt(
        conn,
        submission_id="sub-001",
        user_id="user-1",
        client_attempt_id="att-1",
        ticker="AAPL",
        security_type="STK",
        action="BUY",
        quantity=100,
        limit_price=Decimal("150"),
    )
    await mark_submitted(conn, submission_id="sub-001", ib_order_id=12345, perm_id=99999, placing_client_id=1)
    await mark_terminal(conn, submission_id="sub-001", state="FILLED", filled_qty=100, avg_fill_price=Decimal("149.50"))
    row = await get_by_submission_id(conn, "sub-001")
    assert row["state"] == "FILLED"
    assert row["filled_qty"] == 100


@pytest.mark.asyncio
async def test_record_event(conn):
    from xenon.db.queries.orders import get_events, record_event, reserve_attempt

    await reserve_attempt(
        conn,
        submission_id="sub-001",
        user_id="user-1",
        client_attempt_id="att-1",
        ticker="AAPL",
        security_type="STK",
        action="BUY",
        quantity=100,
        limit_price=Decimal("150"),
    )
    await record_event(conn, submission_id="sub-001", kind="SUBMITTED", detail={"ib_order_id": 12345})
    events = await get_events(conn, submission_id="sub-001")
    assert len(events) == 1
    assert events[0]["kind"] == "SUBMITTED"


@pytest.mark.asyncio
async def test_lookup_by_perm_id(conn):
    from xenon.db.queries.orders import lookup_by_perm_id, mark_submitted, reserve_attempt

    await reserve_attempt(
        conn,
        submission_id="sub-001",
        user_id="user-1",
        client_attempt_id="att-1",
        ticker="AAPL",
        security_type="STK",
        action="BUY",
        quantity=100,
        limit_price=Decimal("150"),
    )
    await mark_submitted(conn, submission_id="sub-001", ib_order_id=12345, perm_id=99999, placing_client_id=1)
    sid = await lookup_by_perm_id(conn, 99999)
    assert sid == "sub-001"


@pytest.mark.asyncio
async def test_lookup_by_ib_order_id(conn):
    from xenon.db.queries.orders import lookup_by_ib_order_id, mark_submitted, reserve_attempt

    await reserve_attempt(
        conn,
        submission_id="sub-001",
        user_id="user-1",
        client_attempt_id="att-1",
        ticker="AAPL",
        security_type="STK",
        action="BUY",
        quantity=100,
        limit_price=Decimal("150"),
    )
    await mark_submitted(conn, submission_id="sub-001", ib_order_id=12345, perm_id=99999, placing_client_id=1)
    sid = await lookup_by_ib_order_id(conn, 12345)
    assert sid == "sub-001"


@pytest.mark.asyncio
async def test_apply_modify(conn):
    from xenon.db.queries.orders import apply_modify, get_by_submission_id, reserve_attempt

    await reserve_attempt(
        conn,
        submission_id="sub-001",
        user_id="user-1",
        client_attempt_id="att-1",
        ticker="AAPL",
        security_type="STK",
        action="BUY",
        quantity=100,
        limit_price=Decimal("150"),
    )
    await apply_modify(conn, submission_id="sub-001", modify_sequence=1)
    row = await get_by_submission_id(conn, "sub-001")
    assert row["modify_sequence"] == 1
