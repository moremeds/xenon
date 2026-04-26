import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_create_engine_returns_async_engine(pg_url):
    from xenon.db.engine import create_engine

    engine = create_engine(pg_url)
    assert isinstance(engine, AsyncEngine)
    await engine.dispose()


@pytest.mark.asyncio
async def test_engine_can_connect(pg_url):
    from xenon.db.engine import create_engine

    engine = create_engine(pg_url)
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar() == 1
    await engine.dispose()
