"""Shared pytest fixtures for src/xenon/api/tests."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

_PG_UNREACHABLE: bool | None = None


@pytest.fixture(autouse=True)
def _postgres_orders_test_db(monkeypatch):
    """Point sync Postgres callers at the test DB and clean order tables.

    Tolerates an unreachable test DB (offline development) — see the matching
    pattern in `scripts/tests/conftest.py`.
    """
    url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )
    monkeypatch.setenv("DATABASE_URL", url)
    sync_url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://")

    try:
        import xenon.db.engine as engine_mod

        monkeypatch.setattr(engine_mod, "_sync_engine", None)
        # Routes that touch the async engine (e.g. GET /portfolio) call
        # `get_engine()` which raises if init_engine() never ran. Tests use
        # TestClient(app) without `with`, so the lifespan that normally
        # initializes it is skipped — seed it here.
        engine_mod._engine = None  # type: ignore[attr-defined]
        engine_mod.init_engine(url)
    except Exception:
        pass

    def truncate() -> None:
        global _PG_UNREACHABLE
        if _PG_UNREACHABLE:
            return
        engine = create_engine(sync_url, pool_pre_ping=True, connect_args={"connect_timeout": 2})
        try:
            with engine.begin() as conn:
                for table in (
                    "events.outbox",
                    "xenon.order_events",
                    "xenon.order_submissions",
                    "xenon.wizard_protection",
                    "xenon.wizard_events",
                    "xenon.wizard_combo_attempts",
                    "xenon.wizard_sessions",
                    "xenon.uw_flow_events",
                    "xenon.uw_api_stats",
                    "xenon.uw_analyze_snapshots",
                    "xenon.positions",
                    "xenon.account_snapshots",
                    "xenon.journal_entries",
                    "xenon.trades",
                    "xenon.nav_history",
                    "xenon.gex_snapshots",
                    "xenon.scan_results",
                    "xenon.cri_series",
                    "xenon.vcg_series",
                    "xenon.ticker_cache",
                ):
                    conn.execute(text(f"TRUNCATE {table} CASCADE"))
        except OperationalError:
            _PG_UNREACHABLE = True
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
