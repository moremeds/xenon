"""M6 — xenon-futu-history-sync CLI.

Tests the orchestration coroutine that the CLI entry point dispatches to.
The argparse glue is trivial; the meat is run_history_sync(scope, since,
client_factory) which sequences M3 → M4 → M5.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from xenon._test_db import is_pg_reachable, sync_test_db_url
from xenon.cli.futu_history_sync import run_history_sync
from xenon.db.schema import futu_cash_flow, futu_trades, nav_history
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="FUTU", account_env="paper", broker_account="pytest")
SINCE = date(2024, 5, 1)


def _fake_client(deals: list[dict], cashflows: list[dict], today_nav: float):
    m = MagicMock()
    m.connect = MagicMock()
    m.disconnect = MagicMock()
    m.fetch_history_deals = MagicMock(return_value=deals)
    m.fetch_capital_flow = MagicMock(return_value=cashflows)
    m.fetch_account = MagicMock(
        return_value={
            # M3 fetch_account returns net_liquidation at the top level
            # (NOT nested under account_summary — that's fetch_portfolio).
            "net_liquidation": today_nav,
            "fetched_at": "2024-05-04T00:00:00Z",
        }
    )
    return m


def _deal(
    deal_id: str, day: str, ticker: str = "AAPL", action: str = "BUY", qty: int = 10, price: float = 100.0
) -> dict:
    return {
        "futu_deal_id": deal_id,
        "futu_order_id": f"o-{deal_id}",
        "ticker": ticker,
        "futu_code": f"US.{ticker}",
        "market": "US",
        "action": action,
        "quantity": float(qty),
        "price": float(price),
        "fees": 0.0,
        "filled_at": datetime.fromisoformat(f"{day}T14:00:00+00:00"),
        "raw": {"trd_side": action, "code": f"US.{ticker}"},
    }


def _flow(flow_id: str, day: str, amount: float, remark: str = "") -> dict:
    return {
        "futu_flow_id": flow_id,
        "cashflow_type": "Others",
        "amount": amount,
        "currency": "USD",
        "occurred_at": datetime.fromisoformat(f"{day}T09:00:00+00:00"),
        "raw": {"cashflow_remark": remark, "currency": "USD"},
    }


@pytest_asyncio.fixture
async def aengine():
    if not is_pg_reachable():
        pytest.skip(f"PG test DB unreachable at {sync_test_db_url()}")
    url = sync_test_db_url().replace("postgresql+psycopg://", "postgresql+asyncpg://")
    eng = create_async_engine(url, pool_pre_ping=True)

    async def _wipe():
        async with eng.begin() as conn:
            for t in (futu_trades, futu_cash_flow, nav_history):
                await conn.execute(
                    sa.delete(t).where(
                        (t.c.broker == "FUTU") & (t.c.account_env == "paper") & (t.c.broker_account == "pytest")
                    )
                )

    await _wipe()
    try:
        yield eng
    finally:
        await _wipe()
        await eng.dispose()


@pytest.mark.asyncio
async def test_cli_runs_full_pipeline(aengine):
    """One BUY + one SELL + one deposit → walks through M3 → M4 → M5."""
    client = _fake_client(
        deals=[
            _deal("d1", "2024-05-01", action="BUY", qty=100, price=100),
            _deal("d2", "2024-05-02", action="SELL", qty=100, price=110),
        ],
        cashflows=[_flow("f1", "2024-05-01", 5000.0, remark="")],
        today_nav=16000.0,
    )

    result = await run_history_sync(
        aengine,
        SCOPE,
        since=SINCE,
        today_date=date(2024, 5, 3),
        client_factory=lambda: client,
    )

    assert result["trades_inserted"] == 2
    assert result["cashflows_inserted"] == 1
    assert result["nav_rows_written"] >= 3  # 2024-05-01 .. 05-03

    # Walk math (nav_by_day[d] = end-of-day NAV after day d's effects):
    #   day 5/3 anchor: 16000
    #   day 5/2 = 16000 - 0 effects on 5/3 = 16000
    #   day 5/1 = 16000 - 1000 realized P&L on 5/2 = 15000
    #   (day 4/30 implicit = 15000 - 0 P&L - 5000 deposit on 5/1 = 10000)
    async with aengine.begin() as conn:
        rows = (
            await conn.execute(
                sa.select(nav_history.c.date, nav_history.c.nav)
                .where((nav_history.c.broker == "FUTU") & (nav_history.c.broker_account == "pytest"))
                .order_by(nav_history.c.date.asc())
            )
        ).fetchall()
    by_day = {r[0]: r[1] for r in rows}
    assert by_day[date(2024, 5, 1)] == Decimal("15000.00")
    assert by_day[date(2024, 5, 2)] == Decimal("16000.00")
    assert by_day[date(2024, 5, 3)] == Decimal("16000.00")


@pytest.mark.asyncio
async def test_cli_disconnects_on_success(aengine):
    client = _fake_client(deals=[], cashflows=[], today_nav=1000.0)
    await run_history_sync(
        aengine,
        SCOPE,
        since=SINCE,
        today_date=date(2024, 5, 3),
        client_factory=lambda: client,
    )
    assert client.disconnect.call_count >= 1


@pytest.mark.asyncio
async def test_cli_disconnects_on_failure(aengine):
    client = _fake_client(deals=[], cashflows=[], today_nav=1000.0)
    client.fetch_account = MagicMock(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await run_history_sync(
            aengine,
            SCOPE,
            since=SINCE,
            today_date=date(2024, 5, 3),
            client_factory=lambda: client,
        )
    assert client.disconnect.call_count >= 1
