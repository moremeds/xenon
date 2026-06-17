"""sync_futu_orders orchestration: persistence, closed-trade rebuild, journal, read-only."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from xenon._test_db import is_pg_reachable, sync_test_db_url
from xenon.api.services.futu_history_sync import sync_futu_orders
from xenon.db.queries.futu_history import list_closed_trades, list_orders
from xenon.db.schema import futu_closed_trades, futu_order_fees, futu_orders, futu_trades, journal_entries
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="FUTU", account_env="paper", broker_account="pytest-sync")
_TABLES = (futu_orders, futu_order_fees, futu_closed_trades, futu_trades, journal_entries)


def _clean(t):
    return sa.delete(t).where((t.c.broker == "FUTU") & (t.c.broker_account == "pytest-sync"))


@pytest_asyncio.fixture
async def clean_scope():
    if not is_pg_reachable():
        pytest.skip(f"PG test DB unreachable at {sync_test_db_url()}")
    url = sync_test_db_url().replace("postgresql+psycopg://", "postgresql+asyncpg://")
    engine = create_async_engine(url, pool_pre_ping=True)
    async with engine.begin() as conn:
        for t in _TABLES:
            await conn.execute(_clean(t))
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            for t in _TABLES:
                await conn.execute(_clean(t))
        await engine.dispose()


def _deal(deal_id, side, qty, price):
    return {
        "futu_deal_id": deal_id,
        "futu_order_id": f"o-{deal_id}",
        "ticker": "QQQ",
        "futu_code": "US.QQQ",
        "market": "US",
        "action": "BUY" if side in ("BUY", "BUY_BACK") else "SELL",
        "quantity": qty,
        "price": price,
        "fees": 0.0,
        "filled_at": datetime.now(timezone.utc),
        "raw": {"trd_side": side, "deal_id": deal_id},
    }


def _order(order_id, status="SUBMITTED"):
    return {
        "futu_order_id": order_id,
        "ticker": "QQQ",
        "futu_code": "US.QQQ",
        "market": "US",
        "action": "BUY",
        "order_type": "NORMAL",
        "quantity": 1,
        "limit_price": 630.96,
        "aux_price": None,
        "status": status,
        "tif": "GTC",
        "filled_qty": 0,
        "avg_fill_price": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "raw": {"order_id": order_id},
    }


def _mock_client(*, deals, open_orders, history_orders=None):
    c = MagicMock()
    c.fetch_history_deals.return_value = deals
    c.fetch_open_orders.return_value = open_orders
    c.fetch_history_orders.return_value = history_orders or []
    c.fetch_order_fees.return_value = [
        {"futu_order_id": o["futu_order_id"], "total_fee": 0.5, "currency": "USD", "raw": {}} for o in open_orders
    ]
    return c


@pytest.mark.asyncio
async def test_sync_persists_orders_fills_and_rebuilds_closed_trades(clean_scope):
    engine = clean_scope
    # BUY then SELL of the same code today → one closed lot + one journal row.
    client = _mock_client(
        deals=[_deal("d1", "BUY", 1, 3.48), _deal("d2", "SELL", 1, 10.40)],
        open_orders=[_order("O1")],
    )
    result = await sync_futu_orders(engine, client, SCOPE)
    assert result["open_orders"] == 1
    assert result["today_fills"] == 2
    assert result["closed_trades"] == 1
    assert result["journal_rows"] == 1

    assert {o["futu_order_id"] for o in await list_orders(engine, SCOPE)} == {"O1"}
    closed = await list_closed_trades(engine, SCOPE)
    assert len(closed) == 1
    assert float(closed[0]["realized_pnl"]) == 6.92  # QQQ stock: (10.40-3.48)*1

    async with engine.begin() as conn:
        jrows = (
            (
                await conn.execute(
                    sa.select(journal_entries).where(
                        (journal_entries.c.broker_account == "pytest-sync")
                        & (journal_entries.c.decision == "FUTU_AUTO_IMPORT")
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(jrows) == 1

    # Idempotent re-run: no duplicate closed trades or journal rows.
    again = await sync_futu_orders(engine, client, SCOPE)
    assert again["closed_trades"] in (0, 1)  # UPSERT may report 0 or 1 depending on rowcount semantics
    assert len(await list_closed_trades(engine, SCOPE)) == 1


@pytest.mark.asyncio
async def test_sync_is_read_only_noop(clean_scope, monkeypatch):
    engine = clean_scope
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    client = _mock_client(deals=[_deal("d1", "BUY", 1, 3.48)], open_orders=[_order("O1")])
    result = await sync_futu_orders(engine, client, SCOPE)
    assert result.get("skipped") == "read_only"
    client.fetch_open_orders.assert_not_called()
    assert await list_orders(engine, SCOPE) == []
