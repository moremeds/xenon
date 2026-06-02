"""M5 — Futu backward NAV walk.

Reads persisted futu_trades + futu_cash_flow, FIFO-matches trades to compute
daily realized P&L, classifies cashflows (Others with empty remark = external),
then walks backward from today's NAV to compute one nav_history row per day.

Tests against synthetic data so the math is exact and reviewable.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from xenon.api.services.futu_nav_backfill import backfill_futu_nav

from xenon._test_db import is_pg_reachable, sync_test_db_url
from xenon.db.queries.futu_history import insert_cashflows, insert_trades
from xenon.db.schema import futu_cash_flow, futu_trades, nav_history
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="FUTU", account_env="paper", broker_account="pytest")


def _t(day: str, code: str, action: str, qty: float, price: float, raw_side: str = None) -> dict:
    """Build a futu_trades row. action is the M3-collapsed side (BUY/SELL);
    raw_side defaults to action."""
    return dict(
        futu_deal_id=f"d-{day}-{code}-{action}-{int(qty)}-{int(price * 100)}",
        futu_order_id=f"o-{day}",
        ticker=code,
        futu_code=f"US.{code}",
        market="US",
        action=action,
        quantity=Decimal(str(qty)),
        price=Decimal(str(price)),
        fees=Decimal("0"),
        filled_at=datetime.fromisoformat(f"{day}T14:00:00+00:00"),
        raw={"trd_side": raw_side or action, "code": f"US.{code}"},
    )


def _f(day: str, ctype: str, amount: float, remark: str = "") -> dict:
    return dict(
        futu_flow_id=f"f-{day}-{ctype}-{int(abs(amount))}",
        cashflow_type=ctype,
        amount=Decimal(str(amount)),
        currency="USD",
        occurred_at=datetime.fromisoformat(f"{day}T09:00:00+00:00"),
        raw={"cashflow_type": ctype, "cashflow_remark": remark, "currency": "USD"},
    )


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


async def _read_nav(eng, scope) -> list[tuple[date, Decimal, Decimal | None]]:
    async with eng.begin() as conn:
        rows = (
            await conn.execute(
                sa.select(nav_history.c.date, nav_history.c.nav, nav_history.c.daily_pnl)
                .where(
                    (nav_history.c.broker == scope.broker)
                    & (nav_history.c.account_env == scope.account_env)
                    & (nav_history.c.broker_account == scope.broker_account)
                )
                .order_by(nav_history.c.date.asc())
            )
        ).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


@pytest.mark.asyncio
async def test_walk_with_only_external_cashflows(aengine):
    """Anchor at $10,000 today. Deposit of $1000 on day 2; nothing else.

    today_nav = 10000
    day 2 NAV = 10000 (after deposit on day 2 — deposits hit on the day they occur)
    day 1 NAV = 10000 - 1000 = 9000 (before deposit)
    """
    await insert_cashflows(aengine, SCOPE, [_f("2024-05-02", "Others", 1000.0, remark="")])
    n = await backfill_futu_nav(
        aengine,
        SCOPE,
        today_nav=Decimal("10000"),
        today_date=date(2024, 5, 3),
        since=date(2024, 5, 1),
    )
    assert n == 3  # 3 days: 5/1, 5/2, 5/3
    rows = await _read_nav(aengine, SCOPE)
    assert rows[0][0] == date(2024, 5, 1)
    assert rows[0][1] == Decimal("9000.00")
    assert rows[1][0] == date(2024, 5, 2)
    assert rows[1][1] == Decimal("10000.00")
    assert rows[2][0] == date(2024, 5, 3)
    assert rows[2][1] == Decimal("10000.00")


@pytest.mark.asyncio
async def test_walk_skips_cashflows_not_classified_as_external(aengine):
    """Cash Dividend, Fund Subscription, etc. do NOT move NAV anchor."""
    await insert_cashflows(
        aengine,
        SCOPE,
        [
            _f("2024-05-02", "Cash Dividend", 50.0, remark="AAPL dividend"),
            _f("2024-05-02", "Fund Subscription", -1000.0, remark="MM fund"),
            _f("2024-05-02", "Others", 200.0, remark="Interest In Apr."),
        ],
    )
    await backfill_futu_nav(
        aengine,
        SCOPE,
        today_nav=Decimal("10000"),
        today_date=date(2024, 5, 3),
        since=date(2024, 5, 1),
    )
    rows = await _read_nav(aengine, SCOPE)
    # All 3 days flat at $10,000 — none of the cashflows count as external
    assert all(r[1] == Decimal("10000.00") for r in rows)


@pytest.mark.asyncio
async def test_walk_realized_pnl_on_stock_round_trip(aengine):
    """BUY 100 AAPL @ $100 on day 1, SELL 100 AAPL @ $110 on day 2.

    Realized P&L on day 2 = (110 - 100) * 100 * 1 = $1000.
    today_nav (day 3) = $11000.
    day 2 NAV = 11000 (after the sell — already includes realized P&L)
    day 1 NAV = 11000 - 1000 = 10000 (before the realized P&L)
    """
    await insert_trades(
        aengine,
        SCOPE,
        [
            _t("2024-05-01", "AAPL", "BUY", 100, 100),
            _t("2024-05-02", "AAPL", "SELL", 100, 110),
        ],
    )
    await backfill_futu_nav(
        aengine,
        SCOPE,
        today_nav=Decimal("11000"),
        today_date=date(2024, 5, 3),
        since=date(2024, 5, 1),
    )
    rows = await _read_nav(aengine, SCOPE)
    by_day = {r[0]: r[1] for r in rows}
    assert by_day[date(2024, 5, 1)] == Decimal("10000.00")
    assert by_day[date(2024, 5, 2)] == Decimal("11000.00")
    assert by_day[date(2024, 5, 3)] == Decimal("11000.00")


@pytest.mark.asyncio
async def test_walk_applies_100x_multiplier_for_options(aengine):
    """OCC ticker → option. 1 contract = 100 shares of underlier.

    BUY 1 AAPL250117C150 @ $5, SELL 1 AAPL250117C150 @ $7.
    Realized P&L = (7 - 5) * 1 * 100 = $200.
    """
    await insert_trades(
        aengine,
        SCOPE,
        [
            _t("2024-05-01", "AAPL250117C150000", "BUY", 1, 5),
            _t("2024-05-02", "AAPL250117C150000", "SELL", 1, 7),
        ],
    )
    await backfill_futu_nav(
        aengine,
        SCOPE,
        today_nav=Decimal("5200"),
        today_date=date(2024, 5, 3),
        since=date(2024, 5, 1),
    )
    rows = await _read_nav(aengine, SCOPE)
    by_day = {r[0]: r[1] for r in rows}
    assert by_day[date(2024, 5, 1)] == Decimal("5000.00")
    assert by_day[date(2024, 5, 2)] == Decimal("5200.00")


@pytest.mark.asyncio
async def test_walk_is_idempotent(aengine):
    await insert_cashflows(aengine, SCOPE, [_f("2024-05-02", "Others", 1000.0)])
    await backfill_futu_nav(
        aengine,
        SCOPE,
        today_nav=Decimal("10000"),
        today_date=date(2024, 5, 3),
        since=date(2024, 5, 1),
    )
    n2 = await backfill_futu_nav(
        aengine,
        SCOPE,
        today_nav=Decimal("10000"),
        today_date=date(2024, 5, 3),
        since=date(2024, 5, 1),
    )
    rows = await _read_nav(aengine, SCOPE)
    assert len(rows) == 3  # no dups on second run
    assert n2 == 3


@pytest.mark.asyncio
async def test_walk_writes_daily_pnl_column(aengine):
    """daily_pnl in nav_history reflects the realized PnL on that day."""
    await insert_trades(
        aengine,
        SCOPE,
        [
            _t("2024-05-01", "AAPL", "BUY", 100, 100),
            _t("2024-05-02", "AAPL", "SELL", 100, 110),
        ],
    )
    await backfill_futu_nav(
        aengine,
        SCOPE,
        today_nav=Decimal("11000"),
        today_date=date(2024, 5, 3),
        since=date(2024, 5, 1),
    )
    rows = await _read_nav(aengine, SCOPE)
    pnl = {r[0]: r[2] for r in rows}
    assert pnl[date(2024, 5, 1)] == Decimal("0")
    assert pnl[date(2024, 5, 2)] == Decimal("1000")
    assert pnl[date(2024, 5, 3)] == Decimal("0")
