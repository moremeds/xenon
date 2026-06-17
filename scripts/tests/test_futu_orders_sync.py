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
async def test_sync_since_widens_deals_window_for_catch_up(clean_scope):
    """A manual refresh passes a back-dated `since` to catch up a multi-day gap.

    The fills pull (step 1) must honor `since`, not stay pinned to today — otherwise
    days between the last sync and now never enter futu_trades and the closed-trade
    rebuild stays stale. `since=None` (the 60s poller) keeps the cheap today window."""
    engine = clean_scope
    since = datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc)
    client = _mock_client(deals=[_deal("d1", "BUY", 1, 3.48)], open_orders=[_order("O1")])
    await sync_futu_orders(engine, client, SCOPE, since=since)
    # Deals (step 1) and history orders (step 2) both fetched from `since`.
    assert client.fetch_history_deals.call_args.kwargs["start"] == since
    assert client.fetch_history_orders.call_args.kwargs["start"] == since


@pytest.mark.asyncio
async def test_sync_since_none_keeps_today_window(clean_scope):
    """`since=None` (poller) pulls only today's fills — the cheap path."""
    from xenon.api.services.futu_history_sync import _today_et_start_utc

    engine = clean_scope
    client = _mock_client(deals=[_deal("d1", "BUY", 1, 3.48)], open_orders=[_order("O1")])
    await sync_futu_orders(engine, client, SCOPE, since=None)
    assert client.fetch_history_deals.call_args.kwargs["start"] == _today_et_start_utc()


def _leg_deal(deal_id, order_id, ticker, side, qty, price):
    return {
        "futu_deal_id": deal_id,
        "futu_order_id": order_id,
        "ticker": ticker,
        "futu_code": f"US.{ticker}",
        "market": "US",
        "action": "BUY" if side in ("BUY", "BUY_BACK") else "SELL",
        "quantity": qty,
        "price": price,
        "fees": 0.0,
        "filled_at": datetime.now(timezone.utc),
        "raw": {"trd_side": side, "deal_id": deal_id},
    }


@pytest.mark.asyncio
async def test_sync_journal_groups_structure_legs_into_one_entry(clean_scope):
    """A 2-leg option structure closed by ONE order → two per-leg closed-trade
    rows (blotter groups on read) but a SINGLE structure-level journal entry,
    ticker = underlying, structure = the classified name."""
    engine = clean_scope
    deals = [
        _leg_deal("o1", "OPEN", "AAOI270115C190000", "BUY", 10, 5.0),
        _leg_deal("o2", "OPEN", "AAOI270115C200000", "SELL_SHORT", 10, 4.0),
        _leg_deal("c1", "CLOSE", "AAOI270115C190000", "SELL", 10, 6.0),
        _leg_deal("c2", "CLOSE", "AAOI270115C200000", "BUY_BACK", 10, 3.0),
    ]
    client = _mock_client(deals=deals, open_orders=[])
    res = await sync_futu_orders(engine, client, SCOPE)
    assert res["closed_trades"] == 2  # per-leg rows persisted for the blotter
    assert res["journal_rows"] == 1  # one structure-level journal entry

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
    j = jrows[0]
    assert j["ticker"] == "AAOI"  # underlying, not the OCC ticker
    assert "Bull Call Spread" in (j["metadata"] or {}).get("structure", "")


@pytest.mark.asyncio
async def test_sync_purges_legacy_per_lot_journal_entries(clean_scope):
    """A pre-grouping per-lot FUTU_AUTO_IMPORT row (old futu_close_id format) is
    purged on the next sync so it doesn't linger alongside the grouped entry."""
    engine = clean_scope
    # Seed a stale legacy per-lot journal entry not in any current grouped set.
    async with engine.begin() as conn:
        await conn.execute(
            sa.insert(journal_entries).values(
                broker="FUTU",
                account_env="paper",
                broker_account="pytest-sync",
                ticker="QQQ250620C500000",
                decision="FUTU_AUTO_IMPORT",
                authored_by="system",
                futu_close_id="staledeal:openlot",
                metadata={"source": "futu_closed_trade", "structure": "Journal Entry"},
            )
        )
    client = _mock_client(
        deals=[_deal("d1", "BUY", 1, 3.48), _deal("d2", "SELL", 1, 10.40)],
        open_orders=[],
    )
    await sync_futu_orders(engine, client, SCOPE)
    async with engine.begin() as conn:
        keys = {
            r[0]
            for r in (
                await conn.execute(
                    sa.select(journal_entries.c.futu_close_id).where(
                        (journal_entries.c.broker_account == "pytest-sync")
                        & (journal_entries.c.decision == "FUTU_AUTO_IMPORT")
                    )
                )
            ).all()
        }
    assert "staledeal:openlot" not in keys  # legacy per-lot row purged


@pytest.mark.asyncio
async def test_sync_is_read_only_noop(clean_scope, monkeypatch):
    engine = clean_scope
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    client = _mock_client(deals=[_deal("d1", "BUY", 1, 3.48)], open_orders=[_order("O1")])
    result = await sync_futu_orders(engine, client, SCOPE)
    assert result.get("skipped") == "read_only"
    client.fetch_open_orders.assert_not_called()
    assert await list_orders(engine, SCOPE) == []
