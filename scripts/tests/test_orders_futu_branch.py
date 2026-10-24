"""X3b Task 2: GET /orders + refresh for FUTU read the execution ledger.

Rows come from `order_submissions` + `order_fills` of the configured Futu
trade scope (FUTU_TRADE_ACC_ID + FUTU_TRADE_ENV), written by a real
observation pass over a fake Futu trade context. `futu_orders` /
`futu_trades` are history only and never read here. 19-digit Futu ids stay
strings; no display hash is returned. All ids, quantities and prices are
made-up test values, not market data.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import update

from scripts.tests.test_futu_observation import (  # noqa: F401 — autouse fixture
    ACC,
    SCOPE,
    FakeTradeCtx,
    _env,
    _order,
    _pos,
    resolver,
)
from xenon.api.routes.orders import orders_payload_for_scope
from xenon.api.services import futu_observation as fo
from xenon.db.engine import get_sync_engine
from xenon.db.queries.futu_history import insert_orders
from xenon.db.schema import order_submissions
from xenon.execution.account_scope import AccountScope

pytestmark = pytest.mark.committed_db

os.environ.setdefault("XENON_API_TEST_MODE", "1")

# A display/history scope that differs from the configured trade scope.
OTHER = AccountScope(broker="FUTU", account_env="live", broker_account="999000111")
WORKING_ID = "8812345678901234567"  # 19 digits: beyond JS Number precision
FILLED_ID = "8812345678901234999"


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("FUTU_TRADE_ACC_ID", ACC)
    monkeypatch.setenv("FUTU_TRADE_ENV", "SIMULATE")


def _observe():
    ctx = FakeTradeCtx(
        positions=[_pos("US.SPY", 100.0)],
        open_orders=[_order(order_id=WORKING_ID, side="SELL", qty=2.0, status="FILLED_PART", dealt=1.0, avg=1.5)],
        history=[
            _order(order_id=FILLED_ID, code="US.SPY", side="BUY", qty=10.0, status="FILLED_ALL", dealt=10.0, avg=2.0)
        ],
    )
    result = fo.run_pass(SCOPE, ctx, resolve_meta=resolver(), audit=False)
    assert result["complete"], result


def test_futu_open_orders_come_from_the_ledger_with_string_ids():
    _observe()
    with get_sync_engine().begin() as conn:
        conn.execute(
            update(order_submissions).where(order_submissions.c.broker_order_id == WORKING_ID).values(modify_sequence=3)
        )

    body = orders_payload_for_scope(SCOPE)

    assert body["scope"] == {"broker": "FUTU", "accountEnv": "paper", "brokerAccount": ACC}
    assert body["open_count"] == 1
    row = body["open_orders"][0]
    assert row["submissionId"] == f"futu-{WORKING_ID}"
    assert row["brokerOrderId"] == WORKING_ID and isinstance(row["brokerOrderId"], str)
    assert row["modifySequence"] == 3
    assert (row["broker"], row["accountEnv"], row["brokerAccount"]) == ("FUTU", "paper", ACC)
    # No display hash: there is no numeric IB id for a Futu order.
    assert row["orderId"] == 0 and row["permId"] == 0
    assert row["status"] == "PartiallyFilled"
    assert (row["totalQuantity"], row["filled"], row["remaining"]) == (2, 1, 1)
    assert row["contract"]["secType"] == "OPT" and row["contract"]["symbol"] == "SPY"


def test_futu_executed_orders_come_from_order_fills():
    _observe()

    body = orders_payload_for_scope(SCOPE)

    by_order = {r["brokerOrderId"]: r for r in body["executed_orders"]}
    assert set(by_order) == {WORKING_ID, FILLED_ID}
    stock = by_order[FILLED_ID]
    assert stock["execId"] == f"futu:{ACC}:ord:{FILLED_ID}:10"
    assert stock["submissionId"] == f"futu-{FILLED_ID}"
    assert (stock["side"], stock["quantity"], stock["avgPrice"]) == ("BOT", 10.0, 2.0)
    assert stock["exchange"] == "FUTU" and stock["broker"] == "FUTU" and stock["brokerAccount"] == ACC
    assert by_order[WORKING_ID]["side"] == "SLD" and by_order[WORKING_ID]["quantity"] == 1.0


@pytest.mark.asyncio
async def test_futu_history_tables_are_not_the_read_path():
    """A futu_orders history row of the scope is not an open order here."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from xenon._test_db import sync_test_db_url

    eng = create_async_engine(sync_test_db_url().replace("postgresql+psycopg://", "postgresql+asyncpg://"))
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
                "limit_price": 1.0,
                "aux_price": None,
                "status": "SUBMITTED",
                "tif": "GTC",
                "filled_qty": 0,
                "avg_fill_price": None,
                "created_at": now,
                "updated_at": now,
                "raw": {},
            }
        ],
    )
    await eng.dispose()

    body = orders_payload_for_scope(SCOPE)

    assert body["open_orders"] == [] and body["executed_orders"] == []


def test_no_configured_scope_returns_empty_with_reason(monkeypatch):
    _observe()
    monkeypatch.delenv("FUTU_TRADE_ACC_ID")

    body = orders_payload_for_scope(SCOPE)

    assert body["open_orders"] == [] and body["executed_orders"] == []
    assert body["scope"] is None and body["scope_reason"] == "FUTU_TRADE_ACC_ID_MISSING"


def test_get_orders_route_serves_ledger_while_opend_offline(monkeypatch):
    """OpenD offline (client never connected): the route still serves the
    persisted ledger rows of the configured scope."""
    from fastapi.testclient import TestClient

    from xenon.api import guards, server
    from xenon.api.routes import futu as futu_routes

    _observe()
    offline = type("Offline", (), {"is_connected": lambda self: False})()
    monkeypatch.setattr(futu_routes, "_get_futu_client", lambda: offline)
    monkeypatch.setattr(guards, "_futu_scope_from_db", lambda: SCOPE)

    resp = TestClient(server.app).get("/orders", params={"broker": "FUTU"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["brokerOrderId"] for r in body["open_orders"]] == [WORKING_ID]
    assert body["scope"]["brokerAccount"] == ACC



@pytest.mark.parametrize(
    "request_scope",
    [
        AccountScope(broker="FUTU", account_env="live", broker_account=ACC),  # REAL tab, SIMULATE configured
        OTHER,  # another account
    ],
)
def test_request_scope_other_than_configured_gets_no_rows(request_scope):
    """Paper and live never blend: the configured scope's orders are served
    only under exactly that scope."""
    _observe()

    body = orders_payload_for_scope(request_scope)

    assert body["open_orders"] == [] and body["executed_orders"] == []
    assert body["open_count"] == 0 and body["executed_count"] == 0
    assert body["scope"] is None and body["scope_reason"] == "scope_mismatch"
    assert body["request_scope"] == {
        "broker": "FUTU",
        "accountEnv": request_scope.account_env,
        "brokerAccount": request_scope.broker_account,
    }
    assert body["configured_scope"] == {"broker": "FUTU", "accountEnv": "paper", "brokerAccount": ACC}


def _route_client(monkeypatch, db_scope):
    from fastapi.testclient import TestClient

    from xenon.api import guards, server
    from xenon.api.routes import futu as futu_routes

    offline = type("Offline", (), {"is_connected": lambda self: False})()
    monkeypatch.setattr(futu_routes, "_get_futu_client", lambda: offline)
    monkeypatch.setattr(guards, "_futu_scope_from_db", lambda: db_scope)
    return TestClient(server.app)


def test_get_orders_route_refuses_a_mismatched_tab_scope(monkeypatch):
    _observe()

    body = _route_client(monkeypatch, OTHER).get("/orders", params={"broker": "FUTU"}).json()

    assert body["open_orders"] == [] and body["scope_reason"] == "scope_mismatch"


@pytest.mark.parametrize("db_scope,expected", [(SCOPE, [WORKING_ID]), (OTHER, [])])
def test_orders_refresh_applies_the_same_scope_rule(monkeypatch, db_scope, expected):
    from xenon.api import server
    from xenon.api.routes import portfolio as portfolio_routes

    _observe()
    refreshed = []

    async def _no_opend(scope):
        refreshed.append(scope)

    monkeypatch.setattr(portfolio_routes, "_futu_refresh_db_first", _no_opend)
    monkeypatch.setattr(server, "_is_test_mode", lambda: False)

    resp = _route_client(monkeypatch, db_scope).post("/orders/refresh", params={"broker": "FUTU"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["brokerOrderId"] for r in body["open_orders"]] == expected
    assert body.get("scope_reason") == (None if expected else "scope_mismatch")
    assert refreshed == [db_scope]


def test_ib_payload_shape_unchanged():
    body = orders_payload_for_scope(AccountScope(broker="IB", account_env="paper", broker_account="DU0000000"))
    assert set(body) == {"last_sync", "open_orders", "executed_orders", "open_count", "executed_count"}

