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

from xenon._test_db import is_pg_reachable, sync_test_db_url
from xenon.db.queries.futu_history import list_cashflows, list_trades
from xenon.db.schema import futu_cash_flow, futu_trades
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


def _flow(flow_id: str, currency: str = "USD", cashflow_type: str = "Others") -> dict:
    return {
        "futu_flow_id": flow_id,
        "cashflow_type": cashflow_type,
        "amount": -1500.0,
        "currency": currency,
        "occurred_at": datetime(2024, 5, 1, 9, tzinfo=timezone.utc),
        "raw": {"cashflow_id": flow_id, "cashflow_remark": ""},
    }


def _mock_client_factory(deals: list[dict], cashflows: list[dict]):
    """Return a factory that yields a mocked FutuClient.

    backfill_history_sync calls client_factory() once, then connect(),
    fetch_history_deals(...), fetch_capital_flow(...), disconnect().
    """

    def _factory():
        m = MagicMock()
        m.connect = MagicMock()
        m.disconnect = MagicMock()
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
        for t in (futu_trades, futu_cash_flow):
            await conn.execute(
                sa.delete(t).where(
                    (t.c.broker == "FUTU") & (t.c.account_env == "paper") & (t.c.broker_account == "pytest")
                )
            )
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            for t in (futu_trades, futu_cash_flow):
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
