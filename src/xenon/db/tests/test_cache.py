from datetime import datetime, timedelta, timezone

import pytest


@pytest.mark.asyncio
async def test_set_and_get_cache(conn):
    from xenon.db.queries.cache import get_cached, set_cached

    await set_cached(conn, ticker="AAPL", cache_type="analyst_ratings", data={"buy": 15, "hold": 5, "sell": 1})
    result = await get_cached(conn, ticker="AAPL", cache_type="analyst_ratings")
    assert result["data"]["buy"] == 15


@pytest.mark.asyncio
async def test_cache_upsert(conn):
    from xenon.db.queries.cache import get_cached, set_cached

    await set_cached(conn, ticker="AAPL", cache_type="company_info", data={"name": "Apple"})
    await set_cached(conn, ticker="AAPL", cache_type="company_info", data={"name": "Apple Inc."})
    result = await get_cached(conn, ticker="AAPL", cache_type="company_info")
    assert result["data"]["name"] == "Apple Inc."


@pytest.mark.asyncio
async def test_expired_cache_returns_none(conn):
    from xenon.db.queries.cache import get_cached, set_cached

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    await set_cached(conn, ticker="AAPL", cache_type="company_info", data={"name": "Apple"}, expires_at=past)
    result = await get_cached(conn, ticker="AAPL", cache_type="company_info")
    assert result is None
