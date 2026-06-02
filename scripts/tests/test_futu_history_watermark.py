"""M8 — incremental watermark resolution for nightly Futu sync.

resolve_incremental_since(engine, scope, inception, lookback_days=7) →

  - No persisted rows → return `inception` (full backfill).
  - Persisted rows → return max(trade.filled_at, cashflow.occurred_at)
    minus lookback_days. The lookback re-covers late-arriving rows
    (dividend tax, post-settlement fee corrections).

Result is a `date`, matching the CLI's `--since` arg shape.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from xenon._test_db import is_pg_reachable, sync_test_db_url
from xenon.api.services.futu_history_sync import resolve_incremental_since
from xenon.db.queries.futu_history import insert_cashflows, insert_trades
from xenon.db.schema import futu_cash_flow, futu_trades
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="FUTU", account_env="paper", broker_account="pytest")
INCEPTION = date(2024, 1, 1)


def _trade(day: str) -> dict:
    return dict(
        futu_deal_id=f"d-{day}",
        futu_order_id=f"o-{day}",
        ticker="AAPL",
        futu_code="US.AAPL",
        market="US",
        action="BUY",
        quantity=Decimal("1"),
        price=Decimal("100"),
        fees=Decimal("0"),
        filled_at=datetime.fromisoformat(f"{day}T14:00:00+00:00"),
        raw={},
    )


def _flow(day: str) -> dict:
    return dict(
        futu_flow_id=f"f-{day}",
        cashflow_type="Others",
        amount=Decimal("100"),
        currency="USD",
        occurred_at=datetime.fromisoformat(f"{day}T09:00:00+00:00"),
        raw={"cashflow_remark": ""},
    )


@pytest_asyncio.fixture
async def aengine():
    if not is_pg_reachable():
        pytest.skip(f"PG test DB unreachable at {sync_test_db_url()}")
    url = sync_test_db_url().replace("postgresql+psycopg://", "postgresql+asyncpg://")
    eng = create_async_engine(url, pool_pre_ping=True)

    async def _wipe():
        async with eng.begin() as conn:
            for t in (futu_trades, futu_cash_flow):
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
async def test_watermark_empty_db_returns_inception(aengine):
    since = await resolve_incremental_since(aengine, SCOPE, inception=INCEPTION)
    assert since == INCEPTION


@pytest.mark.asyncio
async def test_watermark_with_trades_only(aengine):
    """Max trade.filled_at minus lookback (default 7d)."""
    await insert_trades(aengine, SCOPE, [_trade("2024-08-01"), _trade("2025-03-15")])
    since = await resolve_incremental_since(aengine, SCOPE, inception=INCEPTION)
    assert since == date(2025, 3, 8)  # 2025-03-15 minus 7 days


@pytest.mark.asyncio
async def test_watermark_with_cashflows_only(aengine):
    await insert_cashflows(aengine, SCOPE, [_flow("2025-05-20")])
    since = await resolve_incremental_since(aengine, SCOPE, inception=INCEPTION)
    assert since == date(2025, 5, 13)


@pytest.mark.asyncio
async def test_watermark_picks_max_of_both(aengine):
    await insert_trades(aengine, SCOPE, [_trade("2025-01-10")])
    await insert_cashflows(aengine, SCOPE, [_flow("2025-04-20")])
    since = await resolve_incremental_since(aengine, SCOPE, inception=INCEPTION)
    # cashflow (2025-04-20) is later → wins
    assert since == date(2025, 4, 13)


@pytest.mark.asyncio
async def test_watermark_custom_lookback(aengine):
    await insert_trades(aengine, SCOPE, [_trade("2025-05-15")])
    since = await resolve_incremental_since(aengine, SCOPE, inception=INCEPTION, lookback_days=30)
    assert since == date(2025, 4, 15)


@pytest.mark.asyncio
async def test_watermark_scope_isolation(aengine):
    """Other scopes' rows do NOT raise our watermark."""
    other = AccountScope(broker="FUTU", account_env="live", broker_account="pytest")
    await insert_trades(aengine, other, [_trade("2025-12-31")])
    since = await resolve_incremental_since(aengine, SCOPE, inception=INCEPTION)
    assert since == INCEPTION
