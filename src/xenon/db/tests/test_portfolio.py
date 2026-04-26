from datetime import date
from decimal import Decimal

import pytest


@pytest.mark.asyncio
async def test_save_and_get_positions(conn):
    from xenon.db.queries.portfolio import get_positions, save_positions

    positions = [
        {
            "ticker": "AAPL",
            "security_type": "STK",
            "quantity": 100,
            "avg_cost": Decimal("150.25"),
            "current_price": Decimal("155.00"),
            "unrealized_pnl": Decimal("475.00"),
            "account": "IB",
        },
        {
            "ticker": "SPY",
            "security_type": "OPT",
            "expiry": date(2026, 5, 16),
            "strike": Decimal("520.00"),
            "right": "CALL",
            "quantity": 5,
            "avg_cost": Decimal("12.50"),
            "account": "IB",
        },
    ]
    await save_positions(conn, positions, account="IB")
    result = await get_positions(conn, account="IB")
    assert len(result) == 2
    assert result[0]["ticker"] == "AAPL"
    assert result[1]["ticker"] == "SPY"


@pytest.mark.asyncio
async def test_save_positions_replaces_previous(conn):
    from xenon.db.queries.portfolio import get_positions, save_positions

    await save_positions(
        conn,
        [{"ticker": "AAPL", "security_type": "STK", "quantity": 100, "avg_cost": Decimal("150"), "account": "IB"}],
        account="IB",
    )
    await save_positions(
        conn,
        [{"ticker": "MSFT", "security_type": "STK", "quantity": 50, "avg_cost": Decimal("400"), "account": "IB"}],
        account="IB",
    )
    result = await get_positions(conn, account="IB")
    assert len(result) == 1
    assert result[0]["ticker"] == "MSFT"


@pytest.mark.asyncio
async def test_save_account_snapshot(conn):
    from xenon.db.queries.portfolio import get_latest_snapshot, save_account_snapshot

    await save_account_snapshot(
        conn, account="IB", bankroll=Decimal("100000"), peak_value=Decimal("105000"), net_liquidation=Decimal("102000")
    )
    snap = await get_latest_snapshot(conn, account="IB")
    assert snap["bankroll"] == Decimal("100000")


@pytest.mark.asyncio
async def test_upsert_nav(conn):
    from xenon.db.queries.portfolio import get_nav_history, upsert_nav

    today = date(2026, 4, 26)
    await upsert_nav(conn, today, nav=Decimal("100000"), daily_pnl=Decimal("500"))
    await upsert_nav(conn, today, nav=Decimal("100200"), daily_pnl=Decimal("700"))
    history = await get_nav_history(conn)
    assert len(history) == 1
    assert history[0]["nav"] == Decimal("100200")
