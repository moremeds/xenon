"""Shared pytest fixtures for src/xenon/api/tests."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text


@pytest.fixture(autouse=True)
def _postgres_orders_test_db(monkeypatch):
    """Point sync Postgres callers at the test DB and clean order tables."""
    url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )
    sync_url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
    monkeypatch.setenv("DATABASE_URL", sync_url)

    try:
        import xenon.db.engine as engine_mod
        import xenon.execution.orders_store as orders_store_mod

        monkeypatch.setattr(engine_mod, "_sync_engine", None)
        monkeypatch.setattr(orders_store_mod, "_pg_engine", None)
    except Exception:
        pass

    def truncate() -> None:
        engine = create_engine(sync_url, pool_pre_ping=True)
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

    truncate()
    yield
    truncate()


@pytest.fixture(autouse=True)
def _trading_mode_paper_default(monkeypatch):
    """Default every test to paper + a paper-prefixed account so the lifespan
    guard verifies. Tests that care about mismatch override these.

    Why this is autouse + reload-based: tests in `test_health_trading_mode.py`
    and `test_orders_mode_guard.py` reload `trading_mode` (and `server`) with
    `live` to exercise the mismatch path. `importlib.reload` is NOT reverted
    by monkeypatch, so without resetting here, `trading_mode.MODE` would leak
    as `"live"` into subsequent tests — making every paper-prefixed account
    fail `verify_account` and tripping the new 503 guard on routes that have
    nothing to do with trading mode.
    """
    import importlib

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
        # Some tests construct TestClient(app) without a `with` block, so the
        # lifespan that normally seeds app.state never runs. Pre-seed here so
        # require_mode_verified does not trip as a side-effect on routes that
        # have nothing to do with trading mode. Tests that DO run the lifespan
        # (TestClient via `with`) will overwrite these values during startup.
        server.app.state.trading_mode = tm.MODE
        server.app.state.account = "DU0000000"
        server.app.state.mode_verified = True
    except Exception:
        pass
    yield
