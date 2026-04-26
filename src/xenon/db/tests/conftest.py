import os

import pytest
import pytest_asyncio
from sqlalchemy import text

from xenon.db.engine import create_engine
from xenon.db.schema import events_metadata, xenon_metadata


@pytest.fixture
def pg_url():
    return os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )


@pytest_asyncio.fixture
async def engine(pg_url):
    eng = create_engine(pg_url)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def conn(engine):
    async with engine.begin() as connection:
        yield connection
        await connection.rollback()


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(engine):
    """Truncate all tables before each test for isolation."""
    async with engine.begin() as connection:
        for meta in (xenon_metadata, events_metadata):
            for table in reversed(meta.sorted_tables):
                await connection.execute(text(f"TRUNCATE TABLE {table.schema}.{table.name} CASCADE"))
    yield
