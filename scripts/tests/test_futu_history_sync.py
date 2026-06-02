"""M4 sync service: backfill_history_sync — pulls Futu trades + cashflows
and persists via M2 helpers. Uses a mocked FutuClient (dependency-injected
via client_factory) so no live OpenD is required.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from xenon.api.services.futu_history_sync import backfill_history_sync
from xenon.api.services import futu_history_sync

from xenon._test_db import is_pg_reachable, sync_test_db_url
from xenon.db.queries.futu_history import list_cashflows, list_order_fees, list_orders, list_trades
from xenon.db.schema import futu_cash_flow, futu_order_fees, futu_orders, futu_trades
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="FUTU", account_env="paper", broker_account="pytest")
SINCE = datetime(2024, 5, 1, tzinfo=timezone.utc)


def _deal(deal_id: str, market: str = "US", ticker: str = "AAPL") -> dict:
    return {
        "futu_deal_id": deal_id,
        "futu_order_id": f"o-{deal_id}",
        "ticker": ticker,
        "futu_code": f"{market}.{ticker}",
        "market": market,
        "action": "BUY",
        "quantity": 10.0,
        "price": 150.0,
        "fees": 0.0,
        "filled_at": datetime(2024, 5, 1, 14, tzinfo=timezone.utc),
        "raw": {"deal_id": deal_id},
    }


def _combo_deal(deal_id: str, order_id: str, ticker: str, action: str) -> dict:
    row = _deal(deal_id, ticker=ticker)
    row["futu_order_id"] = order_id
    row["action"] = action
    row["raw"] = {"deal_id": deal_id, "order_id": order_id, "trd_side": action}
    return row


def _flow(flow_id: str, currency: str = "USD", cashflow_type: str = "Others") -> dict:
    return {
        "futu_flow_id": flow_id,
        "cashflow_type": cashflow_type,
        "amount": -1500.0,
        "currency": currency,
        "occurred_at": datetime(2024, 5, 1, 9, tzinfo=timezone.utc),
        "raw": {"cashflow_id": flow_id, "cashflow_remark": ""},
    }


def _order(order_id: str, status: str = "FILLED_ALL", ticker: str = "AAPL") -> dict:
    return {
        "futu_order_id": order_id,
        "ticker": ticker,
        "futu_code": f"US.{ticker}",
        "market": "US",
        "trd_side": "BUY",
        "order_type": "NORMAL",
        "order_status": status,
        "quantity": 10.0,
        "price": 150.0,
        "dealt_quantity": 10.0 if status == "FILLED_ALL" else 0.0,
        "dealt_avg_price": 149.5 if status == "FILLED_ALL" else 0.0,
        "currency": "USD",
        "created_at": datetime(2024, 5, 1, 14, tzinfo=timezone.utc),
        "updated_at": datetime(2024, 5, 1, 15, tzinfo=timezone.utc),
        "raw": {"order_id": order_id, "order_status": status},
    }


def _fee(order_id: str, amount: float = 1.23) -> dict:
    return {
        "futu_order_id": order_id,
        "fee_amount": amount,
        "currency": "USD",
        "raw": {"order_id": order_id, "fee_details": [("commission", amount)]},
    }


def _mock_client_factory(deals: list[dict], cashflows: list[dict], orders: list[dict] | None = None, fees: list[dict] | None = None):
    """Return a factory that yields a mocked FutuClient.

    backfill_history_sync calls client_factory() once, then connect(),
    fetch_history_deals(...), fetch_capital_flow(...), disconnect().
    """

    def _factory():
        m = MagicMock()
        m.connect = MagicMock()
        m.disconnect = MagicMock()
        m.fetch_history_orders = MagicMock(return_value=orders or [])
        m.fetch_order_fees = MagicMock(return_value=fees or [])
        m.fetch_history_deals = MagicMock(return_value=deals)
        m.fetch_capital_flow = MagicMock(return_value=cashflows)
        return m

    return _factory


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
                    (t.c.broker == "FUTU") & (t.c.account_env == "paper") & (t.c.broker_account == "pytest")
                )
            )
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            for t in (futu_order_fees, futu_trades, futu_orders, futu_cash_flow):
                await conn.execute(
                    sa.delete(t).where(
                        (t.c.broker == "FUTU") & (t.c.account_env == "paper") & (t.c.broker_account == "pytest")
                    )
                )
        await eng.dispose()


@pytest.mark.asyncio
async def test_sync_persists_deals_and_cashflows(aengine):
    factory = _mock_client_factory(
        deals=[_deal("d1"), _deal("d2", ticker="MSFT")],
        cashflows=[_flow("f1")],
    )
    result = await backfill_history_sync(aengine, SCOPE, since=SINCE, client_factory=factory)
    assert result["trades_inserted"] == 2
    assert result["cashflows_inserted"] == 1
    assert (await list_trades(aengine, SCOPE)) != []
    assert (await list_cashflows(aengine, SCOPE)) != []


@pytest.mark.asyncio
async def test_sync_persists_orders_and_fees(aengine):
    factory = _mock_client_factory(
        deals=[],
        cashflows=[],
        orders=[_order("o1"), _order("o2", status="CANCELLED_ALL")],
        fees=[_fee("o1")],
    )
    result = await backfill_history_sync(aengine, SCOPE, since=SINCE, client_factory=factory)

    assert result["orders_inserted"] == 2
    assert result["order_fees_inserted"] == 1
    assert {row["order_status"] for row in await list_orders(aengine, SCOPE)} == {"FILLED_ALL", "CANCELLED_ALL"}
    assert [row["futu_order_id"] for row in await list_order_fees(aengine, SCOPE)] == ["o1"]


def test_sync_derives_missing_combo_parent_orders_from_deals():
    rows = futu_history_sync._derive_missing_order_rows_from_deals(
        [
            _combo_deal("d1", "combo-1", "TSLA260626P390000", "SELL"),
            _combo_deal("d2", "combo-1", "TSLA260717P390000", "BUY"),
            _combo_deal("d3", "known-order", "AAPL", "BUY"),
        ],
        existing_order_ids={"known-order"},
    )

    assert len(rows) == 1
    assert rows[0]["futu_order_id"] == "combo-1"
    assert rows[0]["source"] == "deal_derived"
    assert rows[0]["is_combo"] is True
    assert rows[0]["leg_count"] == 2
    assert rows[0]["ticker"] is None
    assert rows[0]["order_status"] == "FILLED_ALL"
    assert rows[0]["raw"]["deal_ids"] == ["d1", "d2"]


@pytest.mark.asyncio
async def test_sync_persists_deal_derived_combo_parent_order_and_fee(aengine):
    factory = _mock_client_factory(
        deals=[
            _combo_deal("d1", "combo-1", "TSLA260626P390000", "SELL"),
            _combo_deal("d2", "combo-1", "TSLA260717P390000", "BUY"),
        ],
        cashflows=[],
        orders=[],
        fees=[_fee("combo-1", amount=47.34)],
    )

    result = await backfill_history_sync(aengine, SCOPE, since=SINCE, client_factory=factory)
    orders = await list_orders(aengine, SCOPE)
    fees = await list_order_fees(aengine, SCOPE)

    assert result["orders_derived_from_deals"] == 1
    assert result["orders_inserted"] == 1
    assert orders[0]["source"] == "deal_derived"
    assert orders[0]["is_combo"] is True
    assert orders[0]["leg_count"] == 2
    assert fees[0]["futu_order_id"] == "combo-1"


@pytest.mark.asyncio
async def test_sync_filters_non_us_deals_at_writer(aengine):
    factory = _mock_client_factory(
        deals=[_deal("us1"), _deal("hk1", market="HK", ticker="00700")],
        cashflows=[],
    )
    result = await backfill_history_sync(aengine, SCOPE, since=SINCE, client_factory=factory)
    assert result["trades_inserted"] == 1
    assert result["deals_filtered_non_us"] == 1


@pytest.mark.asyncio
async def test_sync_is_idempotent(aengine):
    factory = _mock_client_factory(deals=[_deal("d1")], cashflows=[_flow("f1")])
    await backfill_history_sync(aengine, SCOPE, since=SINCE, client_factory=factory)
    await backfill_history_sync(aengine, SCOPE, since=SINCE, client_factory=factory)
    assert len(await list_trades(aengine, SCOPE)) == 1
    assert len(await list_cashflows(aengine, SCOPE)) == 1


@pytest.mark.asyncio
async def test_sync_empty_input_is_noop(aengine):
    factory = _mock_client_factory(deals=[], cashflows=[])
    result = await backfill_history_sync(aengine, SCOPE, since=SINCE, client_factory=factory)
    assert result["trades_inserted"] == 0
    assert result["cashflows_inserted"] == 0


@pytest.mark.asyncio
async def test_sync_disconnects_even_on_exception(aengine):
    """If the fetch raises, we still call disconnect() so OpenD doesn't leak."""
    bad_client = MagicMock()
    bad_client.connect = MagicMock()
    bad_client.disconnect = MagicMock()
    bad_client.fetch_history_deals = MagicMock(side_effect=RuntimeError("boom"))
    factory = lambda: bad_client

    with pytest.raises(RuntimeError):
        await backfill_history_sync(aengine, SCOPE, since=SINCE, client_factory=factory)
    bad_client.disconnect.assert_called_once()
