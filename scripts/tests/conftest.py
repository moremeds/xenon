"""Shared pytest configuration and fixtures for scripts tests."""

import importlib
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

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


def _sync_test_db_url() -> str:
    url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


_PG_REACHABLE_CACHE: bool | None = None


def _pg_reachable(engine: Engine) -> bool:
    """Lazily probe PG with a real SELECT 1 + short connect timeout, cache result.

    Lazy because conftest imports BEFORE pytest-dotenv loads .env, so the URL
    available at module-import time may not match the URL the test uses.

    A TCP-only probe is not sufficient: a NAT/firewall may accept the handshake
    while the PG protocol negotiation times out.
    """
    global _PG_REACHABLE_CACHE
    if _PG_REACHABLE_CACHE is not None:
        return _PG_REACHABLE_CACHE
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        _PG_REACHABLE_CACHE = True
    except Exception:
        _PG_REACHABLE_CACHE = False
    return _PG_REACHABLE_CACHE


def _truncate_postgres_tables() -> None:
    global _PG_REACHABLE_CACHE
    if _PG_REACHABLE_CACHE is False:
        return
    engine = create_engine(_sync_test_db_url(), pool_pre_ping=True, connect_args={"connect_timeout": 2})
    try:
        if not _pg_reachable(engine):
            return
        try:
            with engine.begin() as conn:
                for table in (
                    "events.outbox",
                    "xenon.order_fills",
                    "xenon.order_events",
                    "xenon.order_submissions",
                    "xenon.position_close_claims",
                    "xenon.position_protection",
                    "xenon.wizard_events",
                    "xenon.wizard_combo_attempts",
                    "xenon.wizard_sessions",
                    "xenon.uw_flow_event_ticks",
                    "xenon.uw_flow_events",
                    "xenon.uw_api_stats",
                    "xenon.uw_analyze_flow_alerts",
                    "xenon.uw_analyze_gex_strikes",
                    "xenon.uw_analyze_short_volume_trend",
                    "xenon.uw_analyze_snapshots",
                    "xenon.positions",
                    "xenon.account_snapshots",
                    "xenon.journal_entries",
                    "xenon.trades",
                    "xenon.nav_history",
                    "xenon.gex_snapshots",
                    "xenon.scan_results",
                    "xenon.vcg_series",
                    "xenon.cri_series",
                    "xenon.ticker_cache",
                ):
                    conn.execute(text(f"TRUNCATE {table} CASCADE"))
        except SQLAlchemyError:
            _PG_REACHABLE_CACHE = False
    finally:
        engine.dispose()


def _ensure_default_bracket_policies() -> None:
    global _PG_REACHABLE_CACHE
    if _PG_REACHABLE_CACHE is False:
        return
    engine = create_engine(_sync_test_db_url(), pool_pre_ping=True, connect_args={"connect_timeout": 2})
    try:
        if not _pg_reachable(engine):
            return
        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO xenon.bracket_policies (asset_class, rule_kind, auto_place, config) VALUES
                          ('stock',          'stop_loss',          TRUE, '{"threshold_pct": -0.08, "anchor": "entry_price"}'),
                          ('stock',          'trailing_tp',        TRUE, '{"trail_pct": 0.05, "activation_pct": 0.0, "anchor": "mfe"}'),
                          ('long_option',    'stop_loss',          TRUE, '{"threshold_pct": -0.20, "anchor": "entry_price"}'),
                          ('long_option',    'trailing_tp',        TRUE, '{"trail_pct": 0.25, "activation_pct": 0.30, "anchor": "mfe"}'),
                          ('debit_combo',    'stop_loss',          TRUE, '{"threshold_pct_of_max_loss": 0.50, "anchor": "synthetic_mark"}'),
                          ('debit_combo',    'trailing_tp',        TRUE, '{"trail_pct": 0.25, "activation_pct_of_max_gain": 0.25, "anchor": "mfe_pnl_dollars"}'),
                          ('credit_spread',  'stop_loss',          TRUE, '{"trigger_kind": "either", "mark_multiple_of_credit": 2.0, "underlying_breach_short_strike": true, "anchor": "synthetic_mark"}'),
                          ('credit_spread',  'take_profit_fixed',  TRUE, '{"close_at_credit_pct": 0.50, "anchor": "synthetic_mark"}')
                        ON CONFLICT DO NOTHING
                        """
                    )
                )
        except SQLAlchemyError:
            _PG_REACHABLE_CACHE = False
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _postgres_orders_test_db(monkeypatch):
    """Point sync Postgres callers at the test DB and clean order tables.

    Tolerates an unreachable test DB (offline development): truncation is
    silently skipped and tests that actually need PG should depend on the
    `pg_test_engine` fixture, which calls `pytest.skip()` when offline.
    """
    monkeypatch.setenv("DATABASE_URL", _sync_test_db_url())

    try:
        import xenon.db.engine as engine_mod

        monkeypatch.setattr(engine_mod, "_sync_engine", None)
    except Exception:
        pass

    _truncate_postgres_tables()
    _ensure_default_bracket_policies()
    yield
    _truncate_postgres_tables()


@pytest.fixture
def pg_test_engine() -> Engine:
    """Sync SQLAlchemy engine pointed at DATABASE_URL_TEST.

    Skips the test if the test DB is unreachable (offline development).
    Migration tests that need to seed PG should depend on this fixture.
    """
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True, connect_args={"connect_timeout": 2})
    if not _pg_reachable(eng):
        eng.dispose()
        pytest.skip(f"PG test DB unreachable at {_sync_test_db_url()}")
    return eng


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
