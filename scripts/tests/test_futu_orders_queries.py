"""Round-trip + idempotency + scope-isolation for the Futu order query helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from xenon._test_db import is_pg_reachable, sync_test_db_url
from xenon.db.queries.futu_history import (
    insert_closed_trades,
    insert_order_fees,
    insert_orders,
    list_closed_trades,
    list_orders,
)
from xenon.db.schema import futu_closed_trades, futu_order_fees, futu_orders
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="FUTU", account_env="paper", broker_account="pytest")
OTHER = AccountScope(broker="FUTU", account_env="live", broker_account="pytest")
_ALL_TABLES = (futu_orders, futu_order_fees, futu_closed_trades)


def _cleanup_stmt(t):
    return sa.delete(t).where(
        (t.c.broker == "FUTU") & (t.c.account_env.in_(["paper", "live"])) & (t.c.broker_account == "pytest")
    )


@pytest_asyncio.fixture
async def aengine():
    if not is_pg_reachable():
        pytest.skip(f"PG test DB unreachable at {sync_test_db_url()}")
    url = sync_test_db_url().replace("postgresql+psycopg://", "postgresql+asyncpg://")
    eng = create_async_engine(url, pool_pre_ping=True)
    async with eng.begin() as conn:
        for t in _ALL_TABLES:
            await conn.execute(_cleanup_stmt(t))
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            for t in _ALL_TABLES:
                await conn.execute(_cleanup_stmt(t))
        await eng.dispose()


def _order(order_id: str, *, status: str = "SUBMITTED", filled: int = 0, ticker: str = "QQQ") -> dict:
    return dict(
        futu_order_id=order_id,
        ticker=ticker,
        futu_code=f"US.{ticker}",
        market="US",
        action="BUY",
        order_type="NORMAL",
        quantity=1,
        limit_price=630.96,
        aux_price=None,
        status=status,
        tif="GTC",
        filled_qty=filled,
        avg_fill_price=None,
        created_at=datetime(2026, 6, 17, 13, 30, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 17, 13, 31, tzinfo=timezone.utc),
        raw={"order_id": order_id},
    )


@pytest.mark.asyncio
async def test_insert_orders_idempotent_upsert(aengine):
    assert await insert_orders(aengine, SCOPE, [_order("O1")]) == 1
    # Re-pull with a status/fill change → UPSERT in place, not a duplicate row.
    await insert_orders(aengine, SCOPE, [_order("O1", status="FILLED_ALL", filled=1)])
    rows = await list_orders(aengine, SCOPE)
    assert len(rows) == 1
    assert rows[0]["status"] == "FILLED_ALL"
    assert int(rows[0]["filled_qty"]) == 1


@pytest.mark.asyncio
async def test_list_orders_status_filter_and_scope_isolation(aengine):
    await insert_orders(aengine, SCOPE, [_order("A", status="SUBMITTED"), _order("B", status="FILLED_ALL")])
    await insert_orders(aengine, OTHER, [_order("C", status="SUBMITTED")])
    open_only = await list_orders(aengine, SCOPE, statuses={"SUBMITTED"})
    assert {r["futu_order_id"] for r in open_only} == {"A"}
    assert {r["futu_order_id"] for r in await list_orders(aengine, SCOPE)} == {"A", "B"}


@pytest.mark.asyncio
async def test_insert_order_fees_upsert(aengine):
    await insert_order_fees(aengine, SCOPE, [{"futu_order_id": "O1", "total_fee": 0.75, "currency": "USD", "raw": {}}])
    await insert_order_fees(aengine, SCOPE, [{"futu_order_id": "O1", "total_fee": 1.50, "currency": "USD", "raw": {}}])
    async with aengine.begin() as conn:
        rows = (
            (
                await conn.execute(
                    sa.select(futu_order_fees).where(
                        (futu_order_fees.c.broker_account == "pytest") & (futu_order_fees.c.account_env == "paper")
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    assert float(rows[0]["total_fee"]) == 1.50


@pytest.mark.asyncio
async def test_closed_trades_round_trip_and_date_filter(aengine):
    base = dict(
        ticker="QQQ",
        futu_code="US.QQQ",
        structure=None,
        action="SELL",
        quantity=1,
        entry_cost=348,
        exit_cost=1040,
        realized_pnl=692,
        cost_basis=348,
        proceeds=1040,
        opened_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        metadata={},
    )
    await insert_closed_trades(
        aengine,
        SCOPE,
        [
            {**base, "futu_close_id": "x:1", "closed_at": datetime(2026, 6, 10, tzinfo=timezone.utc)},
            {**base, "futu_close_id": "x:2", "closed_at": datetime(2026, 6, 15, tzinfo=timezone.utc)},
        ],
    )
    all_rows = await list_closed_trades(aengine, SCOPE)
    assert {r["futu_close_id"] for r in all_rows} == {"x:1", "x:2"}
    recent = await list_closed_trades(aengine, SCOPE, since=datetime(2026, 6, 12, tzinfo=timezone.utc))
    assert {r["futu_close_id"] for r in recent} == {"x:2"}
    assert float(all_rows[0]["realized_pnl"]) == 692.0
