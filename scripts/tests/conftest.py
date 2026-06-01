"""Shared pytest configuration and fixtures for scripts tests."""

import importlib
import sys
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from xenon._test_db import (
    app_engine_bound_to_test,  # noqa: F401 — re-exported for pytest fixture discovery
    get_session_engine,
    is_pg_reachable,
    pg_session,  # noqa: F401 — re-exported for pytest fixture discovery
    sync_test_db_url,
    truncate_all_xenon_tables,
)
from xenon.execution.account_scope import AccountScope

# Add repo root, scripts/, and src/ so tests can import via:
#   - legacy bare module paths (`from fetchers...`, `from utils...`)
#   - `scripts.*` package paths (historical in a few tests)
#   - new `xenon.*` package paths (Phase 2 reorg destination)
PROJECT_ROOT = Path(__file__).parent.parent.parent
SCRIPTS_DIR = Path(__file__).parent.parent
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SRC_DIR))


# Backwards-compat aliases: a handful of tests import `_sync_test_db_url`
# from this module. The implementation now lives in `_db_fixture`.
_sync_test_db_url = sync_test_db_url


@pytest.fixture(autouse=True)
def _postgres_orders_test_db(monkeypatch):
    """Point sync Postgres callers at the test DB and clean order tables.

    Tolerates an unreachable test DB (offline development): truncation is
    silently skipped and tests that actually need PG should depend on the
    `pg_test_engine` fixture, which calls `pytest.skip()` when offline.
    """
    monkeypatch.setenv("DATABASE_URL", sync_test_db_url())

    try:
        import xenon.db.engine as engine_mod

        monkeypatch.setattr(engine_mod, "_sync_engine", None)
    except Exception:
        pass

    truncate_all_xenon_tables()
    yield
    truncate_all_xenon_tables()


@pytest.fixture
def pg_test_engine() -> Engine:
    """Sync SQLAlchemy engine pointed at DATABASE_URL_TEST.

    Skips the test if the test DB is unreachable (offline development).
    Migration tests that need to seed PG should depend on this fixture.

    Returns the session-scoped shared engine (see `_db_fixture.py`); tests
    must not `.dispose()` it.
    """
    if not is_pg_reachable():
        pytest.skip(f"PG test DB unreachable at {sync_test_db_url()}")
    return get_session_engine()


# Aliases added per perf-rebuild plan correction #6 (2026-06-01).
# Phase 2+ tests use `sync_engine` / `async_engine`; provide both shapes.
@pytest.fixture
def sync_engine(pg_test_engine) -> Engine:
    return pg_test_engine


import pytest_asyncio


@pytest_asyncio.fixture
async def async_engine():
    """Async SQLAlchemy engine pointed at DATABASE_URL_TEST.

    Per-test instance (avoids cross-test connection pool issues). Skips when
    DATABASE_URL_TEST is unreachable. Requires `@pytest_asyncio.fixture`
    (not `@pytest.fixture`) because asyncio_mode is strict.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    url = _sync_test_db_url().replace("postgresql+psycopg://", "postgresql+asyncpg://")
    eng = create_async_engine(url, pool_pre_ping=True)
    try:
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await eng.dispose()
        pytest.skip(f"PG test DB unreachable at {url}")
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def scope_fixture() -> AccountScope:
    """Default paper-mode IB scope used by every migrated CLI test.

    Mirrors the autouse env exports below so tests that build their own
    AccountScope use the same identity that `resolve_from_env()` would yield.
    """
    return AccountScope(broker="IB", account_env="paper", broker_account="DU0000000")


@pytest.fixture(autouse=True)
def _trading_mode_paper_default(monkeypatch):
    """Default every test to paper + a paper-prefixed account so the lifespan
    guard verifies. Mirrors src/xenon/api/tests/conftest.py — needed here too
    because tests in this tree (e.g. test_preflight_route, test_place_quote_gate)
    POST to /orders/place via TestClient(app) without `with`, so the lifespan
    that normally seeds app.state.mode_verified never runs.
    """
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU0000000")
    try:
        import xenon.api.trading_mode as tm

        importlib.reload(tm)
        import xenon.api.server as server

        monkeypatch.setattr(
            server,
            "_get_managed_account_for_health",
            lambda: "DU0000000",
            raising=False,
        )
        server.app.state.trading_mode = tm.MODE
        server.app.state.account = "DU0000000"
        server.app.state.mode_verified = True
    except Exception:
        pass
    yield
