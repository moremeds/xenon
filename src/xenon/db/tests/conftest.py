import os

import pytest
import pytest_asyncio
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from xenon.db.engine import create_engine
from xenon.db.schema import events_metadata, xenon_metadata
from xenon.execution.account_scope import AccountScope


@pytest.fixture
def pg_url():
    return os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )


def _sync_pg_url(pg_url: str) -> str:
    return pg_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


@pytest_asyncio.fixture
async def engine(pg_url):
    eng = create_engine(pg_url)
    try:
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except OperationalError:
        await eng.dispose()
        pytest.skip(f"PG test DB unreachable at {pg_url}")
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def conn(engine):
    async with engine.begin() as connection:
        yield connection
        await connection.rollback()


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(pg_url):
    """Truncate all tables before each test for isolation. Tolerates offline PG."""
    eng = create_engine(pg_url)
    try:
        async with eng.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except OperationalError:
        await eng.dispose()
        yield
        return
    async with eng.begin() as connection:
        for meta in (xenon_metadata, events_metadata):
            for table in reversed(meta.sorted_tables):
                await connection.execute(text(f"TRUNCATE TABLE {table.schema}.{table.name} CASCADE"))
    await eng.dispose()
    yield


@pytest.fixture
def pg_test_engine(pg_url) -> Engine:
    """Sync SQLAlchemy engine pointed at DATABASE_URL_TEST. Skips when offline."""
    sync_url = _sync_pg_url(pg_url)
    eng = create_sync_engine(sync_url, pool_pre_ping=True, connect_args={"connect_timeout": 2})
    try:
        with eng.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError:
        eng.dispose()
        pytest.skip(f"PG test DB unreachable at {sync_url}")
    return eng


@pytest.fixture
def scope_fixture() -> AccountScope:
    return AccountScope(broker="IB", account_env="paper", broker_account="DU0000000")
