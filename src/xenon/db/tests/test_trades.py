from decimal import Decimal

import pytest


@pytest.mark.asyncio
async def test_append_and_get_trades(conn):
    from xenon.db.queries.trades import append_trade, get_journal

    await append_trade(
        conn,
        ticker="AAPL",
        action="BUY",
        quantity=100,
        structure="vertical",
        entry_cost=Decimal("5.00"),
        edge="dark_pool_sweep",
        decision="PASS_ALL_GATES",
    )
    await append_trade(conn, ticker="MSFT", action="SELL", quantity=50, realized_pnl=Decimal("200.00"))
    journal = await get_journal(conn)
    assert len(journal) == 2
    assert journal[0]["ticker"] == "AAPL"


@pytest.mark.asyncio
async def test_get_journal_by_ticker(conn):
    from xenon.db.queries.trades import append_trade, get_journal

    await append_trade(conn, ticker="AAPL", action="BUY", quantity=100)
    await append_trade(conn, ticker="MSFT", action="BUY", quantity=50)
    result = await get_journal(conn, ticker="AAPL")
    assert len(result) == 1
    assert result[0]["ticker"] == "AAPL"
