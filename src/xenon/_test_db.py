"""Shared Postgres test-DB helpers for scripts/tests/ and src/xenon/api/tests/.

Phase 1 of the pytest-suite speedup (`docs/superpowers/plans/2026-06-01-pytest-suite-speedup.md`):
the two conftests used to each `create_engine()` per test and TRUNCATE 26 tables
individually, twice per test. That overhead dominated CI runtime (~872s on the
last full master run). This module centralizes the engine and turns the 26
TRUNCATEs into one statement so the autouse fixture pays one parser/planner
pass and one lock acquisition per test instead of 26.

Semantics are identical to the previous per-test setup:
  - PG unreachable → silently no-op (offline development)
  - Truncate before AND after each test
  - CASCADE drops dependents
  - SQLAlchemyError during truncate (e.g. missing schema) → downgrade
    reachability to False so subsequent tests stop trying
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

XENON_TABLES: tuple[str, ...] = (
    "events.outbox",
    "xenon.order_fills",
    "xenon.order_events",
    "xenon.order_submissions",
    "xenon.wizard_protection",
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
)

# Module-level rather than @lru_cache because the legacy fixture allowed
# downgrading reachability to False mid-session (when TRUNCATE fails with
# SQLAlchemyError, all subsequent tests stop trying). The mutable global
# preserves that contract and lets tests monkeypatch it directly.
_PG_REACHABLE: bool | None = None
_SESSION_ENGINE: Engine | None = None


def sync_test_db_url() -> str:
    """The test DB URL rewritten to use the sync psycopg driver.

    `DATABASE_URL_TEST` is an asyncpg URL by convention (matches the FastAPI
    runtime); sync helpers in this module want the same DB via psycopg.
    """
    url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def get_session_engine() -> Engine:
    """Process-scoped, single SQLAlchemy engine for the entire pytest session.

    Replaces the per-test `create_engine()` that dominated CI runtime. Pool
    sized small (2) because tests are sequential — Phase 3 (pytest-xdist)
    will revisit when per-worker engines land.
    """
    global _SESSION_ENGINE
    if _SESSION_ENGINE is None:
        _SESSION_ENGINE = create_engine(
            sync_test_db_url(),
            pool_pre_ping=True,
            connect_args={"connect_timeout": 2},
            pool_size=2,
            max_overflow=0,
        )
    return _SESSION_ENGINE


def is_pg_reachable() -> bool:
    """Probe PG with a real `SELECT 1`; cache the result for the session.

    A TCP-only probe is insufficient (a NAT/firewall can accept the handshake
    while the PG protocol negotiation times out). Cached so the 2-second
    timeout is paid at most once per pytest invocation.
    """
    global _PG_REACHABLE
    if _PG_REACHABLE is not None:
        return _PG_REACHABLE
    try:
        with get_session_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        _PG_REACHABLE = True
    except SQLAlchemyError:
        _PG_REACHABLE = False
    return _PG_REACHABLE


def mark_offline() -> None:
    """Downgrade reachability cache to False.

    Called when a TRUNCATE fails despite the connectivity probe passing —
    typically a missing schema on a freshly-spun PG instance. Subsequent
    tests then skip truncation entirely and rely on `pg_test_engine`'s
    explicit skip for tests that actually need the DB.
    """
    global _PG_REACHABLE
    _PG_REACHABLE = False


def truncate_all_xenon_tables() -> None:
    """Single-statement TRUNCATE of all xenon.* + events.outbox tables.

    Behavior contract (must match the legacy per-test fixture):
      - PG unreachable → no-op (offline dev workflow stays usable)
      - CASCADE so dependents do not block
      - SQLAlchemyError (e.g. missing schema) → mark offline, do not raise
      - Single statement so the parser, planner, and lock acquisition run
        once instead of 26 times
    """
    if not is_pg_reachable():
        return
    stmt = f"TRUNCATE {', '.join(XENON_TABLES)} CASCADE"
    try:
        with get_session_engine().begin() as conn:
            conn.execute(text(stmt))
    except SQLAlchemyError:
        mark_offline()
