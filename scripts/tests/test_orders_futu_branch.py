"""orders_payload_for_scope FUTU branch: shape parity with IB OpenOrder/ExecutedOrder."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from xenon._test_db import is_pg_reachable, sync_test_db_url
from xenon.api.routes.orders import _futu_contract, _futu_surrogate_id, orders_payload_for_scope
from xenon.db.queries.futu_history import insert_orders
from xenon.db.queries.futu_history import insert_trades as _insert_trades
from xenon.db.schema import futu_orders, futu_trades
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="FUTU", account_env="paper", broker_account="pytest-orders")


def _clean(t):
    return sa.delete(t).where((t.c.broker == "FUTU") & (t.c.broker_account == "pytest-orders"))


@pytest_asyncio.fixture
async def seeded():
    if not is_pg_reachable():
        pytest.skip("PG test DB unreachable")
    url = sync_test_db_url().replace("postgresql+psycopg://", "postgresql+asyncpg://")
    eng = create_async_engine(url, pool_pre_ping=True)
    async with eng.begin() as conn:
        for t in (futu_orders, futu_trades):
            await conn.execute(_clean(t))
    now = datetime.now(timezone.utc)
    await insert_orders(
        eng,
        SCOPE,
        [
            {
                "futu_order_id": "100200300",
                "ticker": "QQQ",
                "futu_code": "US.QQQ",
                "market": "US",
                "action": "BUY",
                "order_type": "NORMAL",
                "quantity": 1,
                "limit_price": 630.96,
                "aux_price": None,
                "status": "SUBMITTED",
                "tif": "GTC",
                "filled_qty": 0,
                "avg_fill_price": None,
                "created_at": now,
                "updated_at": now,
                "raw": {},
            },
            {
                "futu_order_id": "100200999",
                "ticker": "QQQ250620C500000",
                "futu_code": "US.QQQ250620C500000",
                "market": "US",
                "action": "SELL",
                "order_type": "STOP",
                "quantity": 2,
                "limit_price": None,
                "aux_price": 4.5,
                "status": "FILLED_ALL",
                "tif": "DAY",
                "filled_qty": 2,
                "avg_fill_price": 4.5,
                "created_at": now,
                "updated_at": now,
                "raw": {},
            },
        ],
    )
    # A FILLED order that ALSO has a deal — must appear once (via the deal), not
    # double-counted by the order-grain fallback.
    await insert_orders(
        eng,
        SCOPE,
        [
            {
                "futu_order_id": "100200777",
                "ticker": "AAPL",
                "futu_code": "US.AAPL",
                "market": "US",
                "action": "BUY",
                "order_type": "NORMAL",
                "quantity": 3,
                "limit_price": 200.0,
                "aux_price": None,
                "status": "FILLED_ALL",
                "tif": "DAY",
                "filled_qty": 3,
                "avg_fill_price": 199.5,
                "created_at": now,
                "updated_at": now,
                "raw": {},
            },
        ],
    )
    await _insert_trades(
        eng,
        SCOPE,
        [
            {
                "futu_deal_id": "d1",
                "futu_order_id": "100200300",
                "ticker": "QQQ",
                "futu_code": "US.QQQ",
                "market": "US",
                "action": "BUY",
                "quantity": 1,
                "price": 630.0,
                "fees": 0.75,
                "filled_at": now,
                "raw": {"trd_side": "BUY"},
            },
            {
                "futu_deal_id": "d2",
                "futu_order_id": "100200777",
                "ticker": "AAPL",
                "futu_code": "US.AAPL",
                "market": "US",
                "action": "BUY",
                "quantity": 3,
                "price": 199.5,
                "fees": 1.0,
                "filled_at": now,
                "raw": {"trd_side": "BUY"},
            },
        ],
    )
    try:
        yield
    finally:
        async with eng.begin() as conn:
            for t in (futu_orders, futu_trades):
                await conn.execute(_clean(t))
        await eng.dispose()


def test_futu_contract_parses_occ_option():
    c = _futu_contract("QQQ250620C500000")
    assert c["secType"] == "OPT"
    assert c["right"] == "C"
    assert c["strike"] == 500.0
    assert c["expiry"] == "2025-06-20"
    assert c["symbol"] == "QQQ"
    assert _futu_contract("QQQ")["secType"] == "STK"


def test_surrogate_id_is_js_safe_and_distinct():
    a = _futu_surrogate_id("123456789012345678")  # 18-digit
    b = _futu_surrogate_id("123456789012345679")
    assert a != b
    assert a < 2**53  # JS-safe


def test_orders_payload_futu_shapes_like_ib(seeded):
    payload = orders_payload_for_scope(SCOPE)
    # Only the SUBMITTED order is "open"; FILLED_ALL is excluded.
    assert payload["open_count"] == 1
    oo = payload["open_orders"][0]
    assert set(oo) >= {
        "submissionId",
        "symbol",
        "action",
        "orderType",
        "totalQuantity",
        "limitPrice",
        "status",
        "tif",
        "contract",
    }
    assert oo["orderType"] == "LMT"  # NORMAL → LMT
    assert oo["status"] == "Submitted"  # SUBMITTED → Submitted
    assert oo["tif"] == "GTC"
    assert oo["submissionId"] == "100200300"
    # Today's executed orders come from two intraday-available sources:
    #   1. futu_trades deals (deal-grain, with fees) — orders 100200300/100200777
    #   2. FILLED futu_orders with no deal yet (order-grain fallback) — order
    #      100200999 (FILLED_ALL today, no deal) surfaces immediately so today's
    #      fills aren't invisible until the nightly 16:30 ET deal sync.
    by_exec = {eo["execId"]: eo for eo in payload["executed_orders"]}
    assert set(by_exec) == {"d1", "d2", "order-100200999"}
    assert payload["executed_count"] == 3
    # Deal-backed row keeps its fees.
    assert by_exec["d1"]["side"] == "BOT"
    assert by_exec["d1"]["commission"] == 0.75
    # Dedup: order 100200777 is FILLED *and* has deal d2 → appears once (the deal),
    # never as a derived "order-100200777" row.
    assert "order-100200777" not in by_exec
    # The derived row from the dealless FILLED_ALL order (order-grain, prefixed execId).
    derived = by_exec["order-100200999"]
    assert derived["side"] == "SLD"  # SELL → SLD
    assert derived["quantity"] == 2.0  # filled_qty, not total
    assert derived["avgPrice"] == 4.5  # avg_fill_price
    assert derived["commission"] is None  # per-deal fees arrive with the nightly sync
