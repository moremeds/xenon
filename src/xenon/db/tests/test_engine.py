import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from xenon.db.engine import _normalize_pg_url


class TestNormalizePgUrl:
    def test_plain_to_asyncpg(self):
        assert _normalize_pg_url("postgresql://u:p@h/db", driver="asyncpg") == "postgresql+asyncpg://u:p@h/db"

    def test_plain_to_psycopg(self):
        assert _normalize_pg_url("postgresql://u:p@h/db", driver="psycopg") == "postgresql+psycopg://u:p@h/db"

    def test_asyncpg_to_psycopg(self):
        assert _normalize_pg_url("postgresql+asyncpg://u:p@h/db", driver="psycopg") == "postgresql+psycopg://u:p@h/db"

    def test_psycopg_to_asyncpg(self):
        assert _normalize_pg_url("postgresql+psycopg://u:p@h/db", driver="asyncpg") == "postgresql+asyncpg://u:p@h/db"

    def test_asyncpg_stays_asyncpg(self):
        assert _normalize_pg_url("postgresql+asyncpg://u:p@h/db", driver="asyncpg") == "postgresql+asyncpg://u:p@h/db"

    def test_preserves_path_and_params(self):
        url = "postgresql+asyncpg://u:p@host:5432/mydb?sslmode=require"
        assert _normalize_pg_url(url, driver="psycopg") == "postgresql+psycopg://u:p@host:5432/mydb?sslmode=require"


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
