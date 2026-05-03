import os

import pytest
import pytest_asyncio
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from xenon.db.engine import create_engine
from xenon.db.schema import events_metadata, xenon_metadata
from xenon.execution.account_scope import AccountScope


def _default_pg_url() -> str:
    return os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )


def _sync_pg_url(pg_url: str) -> str:
    return pg_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


_PG_REACHABLE_CACHE: bool | None = None


def _pg_reachable(pg_url: str) -> bool:
    """Lazily probe PG with a real SELECT 1 + short connect timeout, cache result.

    Lazy because conftest imports BEFORE pytest-dotenv loads .env, so the URL
    available at module-import time may not match the URL the test uses.

    A TCP-only probe is not sufficient: a NAT/firewall may accept the handshake
    while the PG protocol negotiation times out.
    """
    global _PG_REACHABLE_CACHE
    if _PG_REACHABLE_CACHE is not None:
        return _PG_REACHABLE_CACHE
    sync_url = _sync_pg_url(pg_url)
    try:
        eng = create_sync_engine(sync_url, pool_pre_ping=False, connect_args={"connect_timeout": 2})
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        eng.dispose()
        _PG_REACHABLE_CACHE = True
    except Exception:
        _PG_REACHABLE_CACHE = False
    return _PG_REACHABLE_CACHE


@pytest.fixture
def pg_url():
    return _default_pg_url()


@pytest_asyncio.fixture
async def engine(pg_url):
    if not _pg_reachable(pg_url):
        pytest.skip(f"PG test DB unreachable at {pg_url}")
    eng = create_engine(pg_url)
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
    global _PG_REACHABLE_CACHE
    if not _pg_reachable(pg_url):
        yield
        return
    eng = create_engine(pg_url)
    try:
        async with eng.begin() as connection:
            for meta in (xenon_metadata, events_metadata):
                for table in reversed(meta.sorted_tables):
                    await connection.execute(text(f"TRUNCATE TABLE {table.schema}.{table.name} CASCADE"))
    except SQLAlchemyError:
        _PG_REACHABLE_CACHE = False
    finally:
        await eng.dispose()
    yield


@pytest.fixture
def pg_test_engine(pg_url) -> Engine:
    """Sync SQLAlchemy engine pointed at DATABASE_URL_TEST. Skips when offline."""
    sync_url = _sync_pg_url(pg_url)
    if not _pg_reachable(pg_url):
        pytest.skip(f"PG test DB unreachable at {sync_url}")
    return create_sync_engine(sync_url, pool_pre_ping=True, connect_args={"connect_timeout": 2})


@pytest.fixture
def scope_fixture() -> AccountScope:
    return AccountScope(broker="IB", account_env="paper", broker_account="DU0000000")
