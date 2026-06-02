"""Round-trip + scope-filter tests for xenon.db.queries.futu_history.

Hits the real test DB via pytest_asyncio. PG-unreachable skips per the
existing conftest pattern.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from xenon._test_db import is_pg_reachable, sync_test_db_url
from xenon.db.queries.futu_history import (
    insert_cashflows,
    insert_order_fees,
    insert_orders,
    insert_trades,
    list_cashflows,
    list_order_fees,
    list_orders,
    list_trades,
)
from xenon.db.schema import futu_cash_flow, futu_order_fees, futu_orders, futu_trades
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="FUTU", account_env="paper", broker_account="pytest")
OTHER_SCOPE = AccountScope(broker="FUTU", account_env="live", broker_account="pytest")


@pytest_asyncio.fixture
async def aengine():
    if not is_pg_reachable():
        pytest.skip(f"PG test DB unreachable at {sync_test_db_url()}")
    url = sync_test_db_url().replace("postgresql+psycopg://", "postgresql+asyncpg://")
    eng = create_async_engine(url, pool_pre_ping=True)
    async with eng.begin() as conn:
        for t in (futu_order_fees, futu_trades, futu_orders, futu_cash_flow):
            await conn.execute(
                sa.delete(t).where(
                    (t.c.broker == "FUTU") & (t.c.account_env.in_(["paper", "live"])) & (t.c.broker_account == "pytest")
                )
            )
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            for t in (futu_order_fees, futu_trades, futu_orders, futu_cash_flow):
                await conn.execute(
                    sa.delete(t).where(
                        (t.c.broker == "FUTU")
                        & (t.c.account_env.in_(["paper", "live"]))
                        & (t.c.broker_account == "pytest")
                    )
                )
        await eng.dispose()


def _trade(deal_id: str, ticker: str = "AAPL", action: str = "BUY", qty: int = 10, price: str = "150") -> dict:
    return dict(
        futu_deal_id=deal_id,
        futu_order_id=f"o-{deal_id}",
        ticker=ticker,
        futu_code=f"US.{ticker}",
        market="US",
        action=action,
        quantity=Decimal(qty),
        price=Decimal(price),
        fees=Decimal("1"),
        filled_at=datetime(2024, 5, 1, 14, 30, tzinfo=timezone.utc),
        raw={"deal_id": deal_id},
    )


def _flow(flow_id: str, ctype: str = "DEPOSIT", amount: str = "1000") -> dict:
    return dict(
        futu_flow_id=flow_id,
        cashflow_type=ctype,
        amount=Decimal(amount),
        currency="USD",
        occurred_at=datetime(2024, 5, 1, 9, 0, tzinfo=timezone.utc),
        raw={"flow_id": flow_id},
    )


def _order(order_id: str, ticker: str = "AAPL", side: str = "BUY") -> dict:
    return dict(
        futu_order_id=order_id,
        ticker=ticker,
        futu_code=f"US.{ticker}",
        market="US",
        trd_side=side,
        order_type="NORMAL",
        order_status="FILLED_ALL",
        quantity=Decimal("10"),
        price=Decimal("150"),
        dealt_quantity=Decimal("10"),
        dealt_avg_price=Decimal("149.5"),
        currency="USD",
        created_at=datetime(2024, 5, 1, 14, 0, tzinfo=timezone.utc),
        updated_at=datetime(2024, 5, 1, 14, 1, tzinfo=timezone.utc),
        raw={"order_id": order_id},
    )


def _fee(order_id: str, amount: str = "1.23") -> dict:
    return dict(
        futu_order_id=order_id,
        fee_amount=Decimal(amount),
        currency="USD",
        raw={"order_id": order_id, "fee_details": [{"fee_type": "commission", "amount": amount}]},
    )


def _orders_for_trades(rows: list[dict]) -> list[dict]:
    return [_order(str(row["futu_order_id"]), ticker=row["ticker"], side=row["action"]) for row in rows]


@pytest.mark.asyncio
async def test_insert_trades_round_trip(aengine):
    rows = [_trade("d1"), _trade("d2", action="SELL")]
    await insert_orders(aengine, SCOPE, _orders_for_trades(rows))
    await insert_trades(aengine, SCOPE, rows)
    rows = await list_trades(aengine, SCOPE)
    assert {r["futu_deal_id"] for r in rows} == {"d1", "d2"}


@pytest.mark.asyncio
async def test_insert_trades_idempotent(aengine):
    await insert_orders(aengine, SCOPE, _orders_for_trades([_trade("d1")]))
    await insert_trades(aengine, SCOPE, [_trade("d1")])
    await insert_trades(aengine, SCOPE, [_trade("d1", price="999")])  # re-pull
    rows = await list_trades(aengine, SCOPE)
    assert len(rows) == 1
    # UPSERT should win — most recent values replace
    assert rows[0]["price"] == Decimal("999.0000")


@pytest.mark.asyncio
async def test_list_trades_scope_filter(aengine):
    scope_rows = [_trade("d1")]
    other_rows = [_trade("d2")]
    await insert_orders(aengine, SCOPE, _orders_for_trades(scope_rows))
    await insert_orders(aengine, OTHER_SCOPE, _orders_for_trades(other_rows))
    await insert_trades(aengine, SCOPE, scope_rows)
    await insert_trades(aengine, OTHER_SCOPE, other_rows)
    rows = await list_trades(aengine, SCOPE)
    assert {r["futu_deal_id"] for r in rows} == {"d1"}


@pytest.mark.asyncio
async def test_list_trades_date_range(aengine):
    t_old = _trade("d_old")
    t_old["filled_at"] = datetime(2024, 1, 1, 14, 0, tzinfo=timezone.utc)
    t_mid = _trade("d_mid")
    t_mid["filled_at"] = datetime(2024, 6, 15, 14, 0, tzinfo=timezone.utc)
    t_new = _trade("d_new")
    t_new["filled_at"] = datetime(2025, 1, 1, 14, 0, tzinfo=timezone.utc)
    rows = [t_old, t_mid, t_new]
    await insert_orders(aengine, SCOPE, _orders_for_trades(rows))
    await insert_trades(aengine, SCOPE, rows)
    mid_only = await list_trades(
        aengine,
        SCOPE,
        since=datetime(2024, 5, 1, tzinfo=timezone.utc),
        until=datetime(2024, 12, 31, tzinfo=timezone.utc),
    )
    assert {r["futu_deal_id"] for r in mid_only} == {"d_mid"}


@pytest.mark.asyncio
async def test_insert_cashflows_round_trip(aengine):
    await insert_cashflows(aengine, SCOPE, [_flow("f1"), _flow("f2", ctype="WITHDRAW", amount="-500")])
    rows = await list_cashflows(aengine, SCOPE)
    assert {r["futu_flow_id"] for r in rows} == {"f1", "f2"}


@pytest.mark.asyncio
async def test_insert_orders_and_fees_round_trip(aengine):
    await insert_orders(aengine, SCOPE, [_order("o1"), _order("o2", ticker="MSFT", side="SELL")])
    await insert_order_fees(aengine, SCOPE, [_fee("o1"), _fee("o2", amount="2.34")])

    orders = await list_orders(aengine, SCOPE)
    fees = await list_order_fees(aengine, SCOPE)

    assert {r["futu_order_id"] for r in orders} == {"o1", "o2"}
    assert {r["futu_order_id"] for r in fees} == {"o1", "o2"}
    assert sum(r["fee_amount"] for r in fees) == Decimal("3.5700")


@pytest.mark.asyncio
async def test_insert_cashflows_idempotent(aengine):
    await insert_cashflows(aengine, SCOPE, [_flow("f1")])
    await insert_cashflows(aengine, SCOPE, [_flow("f1", amount="2000")])
    rows = await list_cashflows(aengine, SCOPE)
    assert len(rows) == 1
    assert rows[0]["amount"] == Decimal("2000.0000")


@pytest.mark.asyncio
async def test_list_cashflows_scope_filter(aengine):
    await insert_cashflows(aengine, SCOPE, [_flow("f1")])
    await insert_cashflows(aengine, OTHER_SCOPE, [_flow("f2")])
    rows = await list_cashflows(aengine, SCOPE)
    assert {r["futu_flow_id"] for r in rows} == {"f1"}


@pytest.mark.asyncio
async def test_insert_trades_chunks_past_postgres_bind_param_cap(aengine):
    """Bulk inserts must chunk so a single statement never exceeds the
    32767-bind-param Postgres wire protocol cap. 6000 rows × 15 cols =
    90,000 params — only survives if the writer batches internally.
    """
    rows = [_trade(f"bulk-{i}") for i in range(6000)]
    await insert_orders(aengine, SCOPE, _orders_for_trades(rows))
    n = await insert_trades(aengine, SCOPE, rows)
    assert n >= 6000
    assert len(await list_trades(aengine, SCOPE)) == 6000


@pytest.mark.asyncio
async def test_empty_input_is_noop(aengine):
    await insert_trades(aengine, SCOPE, [])
    await insert_cashflows(aengine, SCOPE, [])
    assert await list_trades(aengine, SCOPE) == []
    assert await list_cashflows(aengine, SCOPE) == []
