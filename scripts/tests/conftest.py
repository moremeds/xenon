"""Shared pytest configuration and fixtures for scripts tests."""

import importlib
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

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


def _sync_test_db_url() -> str:
    url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def _truncate_postgres_tables() -> None:
    engine = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    try:
        with engine.begin() as conn:
            for table in (
                "xenon.order_events",
                "xenon.order_submissions",
                "xenon.wizard_protection",
                "xenon.wizard_events",
                "xenon.wizard_combo_attempts",
                "xenon.wizard_sessions",
                "xenon.uw_flow_events",
                "xenon.uw_api_stats",
            ):
                conn.execute(text(f"TRUNCATE {table} CASCADE"))
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _postgres_orders_test_db(monkeypatch):
    """Point sync Postgres callers at the test DB and clean order tables."""
    monkeypatch.setenv("DATABASE_URL", _sync_test_db_url())

    try:
        import xenon.db.engine as engine_mod

        monkeypatch.setattr(engine_mod, "_sync_engine", None)
    except Exception:
        pass

    _truncate_postgres_tables()
    yield
    _truncate_postgres_tables()


@pytest.fixture(autouse=True)
def _trading_mode_paper_default(monkeypatch):
    """Default every test to paper + a paper-prefixed account so the lifespan
    guard verifies. Mirrors src/xenon/api/tests/conftest.py — needed here too
    because tests in this tree (e.g. test_preflight_route, test_place_quote_gate)
    POST to /orders/place via TestClient(app) without `with`, so the lifespan
    that normally seeds app.state.mode_verified never runs.
    """
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
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
