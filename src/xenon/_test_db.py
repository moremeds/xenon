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
  - Connection error during truncate → downgrade reachability to False so
    subsequent tests stop trying; any other SQLAlchemyError re-raises

Prod-DB guard (INV-test-db-leak): `bind_database_url_to_test_db()` runs at
root-conftest import, before any xenon import, and points `DATABASE_URL` at
the test DB so import-time singletons never bind to the developer's
`DATABASE_URL` (e.g. prod `core_dev` from `.env`).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError

# Eagerly load `.env` so DATABASE_URL_TEST is set before any fixture (esp.
# the session-scoped `_ensure_worker_db`) runs. In serial pytest this is
# redundant — ib_client's import-time `load_dotenv()` already fired — but in
# xdist workers the session-scoped autouse runs BEFORE test collection
# (which is when ib_client gets imported), so without this the worker DB
# clone would target the hardcoded fallback (`xenon_test`) instead of
# whatever the user has in `.env`. CI: `.env` doesn't exist → no-op.
try:
    from dotenv import load_dotenv as _load_dotenv

    _ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
    if _ENV_FILE.exists():
        _load_dotenv(_ENV_FILE)
except ImportError:
    pass

XENON_TABLES: tuple[str, ...] = (
    "events.outbox",
    "xenon.order_fills",
    "xenon.order_events",
    "xenon.order_submissions",
    "xenon.wizard_protection",
    "xenon.wizard_events",
    "xenon.wizard_combo_attempts",
    "xenon.wizard_sessions",
    "xenon.positions",
    "xenon.account_snapshots",
    "xenon.journal_entries",
    "xenon.trades",
    "xenon.nav_history",
    "xenon.service_health",
    "xenon.flex_divergence_runs",
    "xenon.ib_cash_flow",
    "xenon.regime_overrides",
    "xenon.benchmark_closes",
    "xenon.user_watchlist",
    "xenon.futu_cash_flow",
    "xenon.futu_daily_statement",
    "xenon.futu_statement_inbox",
    "xenon.futu_trades",
    "xenon.futu_orders",
    "xenon.futu_order_fees",
    "xenon.futu_closed_trades",
)

# Module-level rather than @lru_cache because the legacy fixture allowed
# downgrading reachability to False mid-session (when TRUNCATE fails with
# SQLAlchemyError, all subsequent tests stop trying). The mutable global
# preserves that contract and lets tests monkeypatch it directly.
_PG_REACHABLE: bool | None = None
_SESSION_ENGINE: Engine | None = None


def _resolve_worker_id(worker_id: str | None) -> str | None:
    """Return the xdist worker id, or None for serial mode.

    Phase 3: `pytest-xdist` exports `PYTEST_XDIST_WORKER` (e.g. "gw0") inside
    each spawned worker; serial pytest leaves the env unset OR sets it to
    "master" (xdist's sentinel for the controller). Both serial cases must
    resolve to None so the URL stays on the unsuffixed `xenon_test`.
    """
    wid = worker_id if worker_id is not None else os.environ.get("PYTEST_XDIST_WORKER")
    if not wid or wid == "master":
        return None
    return wid


def async_test_db_url(worker_id: str | None = None) -> str:
    """Async (asyncpg) form of the test DB URL — used by FastAPI route tests
    and the async_engine fixture. Inverts the psycopg-rewrite that
    `sync_test_db_url` applies, so worker suffix and DB name stay in sync.
    """
    return sync_test_db_url(worker_id).replace("postgresql+psycopg://", "postgresql+asyncpg://")


def sync_test_db_url(worker_id: str | None = None) -> str:
    """The test DB URL rewritten to use the sync psycopg driver.

    `DATABASE_URL_TEST` is an asyncpg URL by convention (matches the FastAPI
    runtime); sync helpers in this module want the same DB via psycopg.

    Phase 3: when running under pytest-xdist, each worker gets its own
    per-worker DB (`xenon_test_gw0`, `xenon_test_gw1`, ...) so workers do not
    contend on the same WAL / lock table. Worker resolution comes from
    `PYTEST_XDIST_WORKER` (set automatically by xdist) unless the caller
    supplies it explicitly. Rewrites only the final path segment so DB names
    that contain `xenon_test` mid-string are not mangled.
    """
    url = _base_test_db_url(os.environ).replace("postgresql+asyncpg://", "postgresql+psycopg://")
    return _with_worker_suffix(url, _resolve_worker_id(worker_id))


_DEFAULT_TEST_DB_URL = "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test"


def _base_test_db_url(environ: Any) -> str:
    return environ.get("DATABASE_URL_TEST", _DEFAULT_TEST_DB_URL)


def _with_worker_suffix(url: str, wid: str | None) -> str:
    """Suffix the parsed database name with `_<wid>`; keep the query string.

    Only `URL.database` changes: a raw string split would suffix the last
    query value instead (`?application_name=pytest_gw0`) and leave every
    worker on the shared DB. Idempotent: the autouse fixture rewrites
    DATABASE_URL_TEST in env to the worker URL, and the next call reads that
    already-suffixed value — it must not become `xenon_test_gw0_gw0`.
    """
    if wid is None:
        return url
    from sqlalchemy.engine.url import make_url

    parsed = make_url(url)
    dbname = parsed.database or ""
    suffix = f"_{wid}"
    if dbname.endswith(suffix):
        return url
    return parsed.set(database=f"{dbname}{suffix}").render_as_string(hide_password=False)


def _without_worker_suffix(url: str, wid: str) -> str:
    from sqlalchemy.engine.url import make_url

    parsed = make_url(url)
    dbname = (parsed.database or "").removesuffix(f"_{wid}")
    return parsed.set(database=dbname).render_as_string(hide_password=False)


# ---------------------------------------------------------------------------
# Prod-DB guard
# ---------------------------------------------------------------------------

# Identity (`host/dbname`, no credentials) of the DATABASE_URL the developer
# had before the guard rewrote it. Set once by the first pytest process and
# inherited by xdist workers, whose DATABASE_URL is already the rewritten one.
DB_GUARD_ORIGINAL_ENV = "XENON_TEST_DB_GUARD_ORIGINAL"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class UnsafeTestDatabaseError(RuntimeError):
    """The test DB could be a non-test database; the session must not run."""


# Query keys a test or original DB URL may carry. Anything else (`dbname`,
# `host`, asyncpg `dsn`, `service`, ...) can re-route the connection away from
# the URL's own host/dbname, so the guard refuses it: allowlist, not blocklist.
_ALLOWED_QUERY_KEYS = frozenset({"sslmode", "sslrootcert", "sslcert", "sslkey", "connect_timeout", "application_name"})


def _disallowed_query_keys(url: str) -> list[str]:
    from sqlalchemy.engine.url import make_url

    return sorted(k for k in make_url(url).query if k not in _ALLOWED_QUERY_KEYS)


def _refuse_disallowed_query(url: str, var: str) -> None:
    keys = _disallowed_query_keys(url)
    if keys:
        raise UnsafeTestDatabaseError(
            f"Refusing to run pytest: {var} has query params {keys} outside the "
            f"allowlist {sorted(_ALLOWED_QUERY_KEYS)}; they can re-route the connection "
            f"(routing) to a different server or database than the URL shows. {_GUARD_FIX_HINT}"
        )


def _db_identity(url: str) -> str:
    """`host/dbname` of a DB URL. Loopback aliases collapse to one host; an
    empty host (socket / PGHOST routed) stays empty: `/dbname`."""
    from sqlalchemy.engine.url import make_url

    parsed = make_url(url)
    host = (parsed.host or "").lower()
    if host in _LOOPBACK_HOSTS:
        host = "localhost"
    return f"{host}/{parsed.database or ''}"


_GUARD_FIX_HINT = (
    "Fix: export DATABASE_URL_TEST to a separate local test database whose name "
    "contains 'test' (CI uses postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/"
    "xenon_test), and make sure it is not the DATABASE_URL database (exported or "
    "from .env). pytest replaces DATABASE_URL with the test URL for the session."
)


def bind_database_url_to_test_db(environ: Any = None) -> str:
    """Point `DATABASE_URL` at the test DB, or refuse the session.

    Called at root-conftest import, before any xenon module that caches an
    engine is imported. Refuses (raises `UnsafeTestDatabaseError`) when the
    test DB name is `core_dev` or lacks `test`, when the test DB has the
    same host+dbname as the original `DATABASE_URL`, when either URL has a
    query key outside `_ALLOWED_QUERY_KEYS`, or when only one of the two URLs
    has an empty host (both empty: dbname alone is compared). Returns the bound
    (asyncpg, worker-suffixed under xdist) URL.
    """
    env = os.environ if environ is None else environ
    if DB_GUARD_ORIGINAL_ENV not in env:
        original = env.get("DATABASE_URL", "")
        if original:
            _refuse_disallowed_query(original, "DATABASE_URL")
        env[DB_GUARD_ORIGINAL_ENV] = _db_identity(original) if original else ""
    original_identity = env[DB_GUARD_ORIGINAL_ENV]

    from sqlalchemy.engine.url import make_url

    base = _base_test_db_url(env).replace("postgresql+psycopg://", "postgresql+asyncpg://")
    _refuse_disallowed_query(base, "DATABASE_URL_TEST")
    bound = _with_worker_suffix(base, _resolve_worker_id(env.get("PYTEST_XDIST_WORKER", "")))
    for url in (base, bound):
        dbname = make_url(url).database or ""
        if dbname == "core_dev" or "test" not in dbname.lower():
            raise UnsafeTestDatabaseError(
                f"Refusing to run pytest: test DB name {dbname!r} (DATABASE_URL_TEST) "
                f"must contain 'test' and must not be core_dev. {_GUARD_FIX_HINT}"
            )
        if not original_identity:
            continue
        identity = _db_identity(url)
        if identity.startswith("/") != original_identity.startswith("/"):
            raise UnsafeTestDatabaseError(
                "Refusing to run pytest: only one of DATABASE_URL / DATABASE_URL_TEST "
                "has a host; an empty host routes via socket or PGHOST and cannot be "
                f"compared. Give both URLs a host. {_GUARD_FIX_HINT}"
            )
        if identity == original_identity:
            raise UnsafeTestDatabaseError(
                f"Refusing to run pytest: DATABASE_URL_TEST ({identity}) is the "
                f"same database as DATABASE_URL. {_GUARD_FIX_HINT}"
            )
    env["DATABASE_URL"] = bound
    return bound


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


_SEQ_ENGINE: Engine | None = None


def committed_pass_stamp() -> dict[str, str]:
    """Gate-4 `pass_seq` + `pass_cluster` from their own committed transaction.

    Same values ib_sync stamps via `gate4.next_pass_seq`. A dedicated
    NullPool connection keeps the xid out of the test's open outer
    transaction (seeds then order like real, committed passes) and never
    takes a session-pool slot.
    """
    global _SEQ_ENGINE
    if _SEQ_ENGINE is None:
        from sqlalchemy.pool import NullPool

        _SEQ_ENGINE = create_engine(sync_test_db_url(), poolclass=NullPool)
    with _SEQ_ENGINE.begin() as conn:
        seq, cluster = conn.execute(
            text("SELECT pg_current_xact_id()::text, (SELECT system_identifier FROM pg_control_system())::text")
        ).one()
    return {"pass_seq": seq, "pass_cluster": cluster}


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
    except SQLAlchemyError as exc:
        # Only "cannot connect" is offline. Anything else (permission denied,
        # a bad statement) re-raises instead of silently skipping PG tests.
        if not _is_connection_error(exc):
            raise
        _PG_REACHABLE = False
    return _PG_REACHABLE


def mark_offline() -> None:
    """Downgrade reachability cache to False.

    Called when a TRUNCATE hits a connection error after the probe passed
    (PG went away mid-session). A missing schema is not offline: TRUNCATE
    re-raises it and `_pg_schema_guard` reports the missing tables.
    Subsequent tests skip truncation and rely on `pg_test_engine`'s explicit
    skip for tests that actually need the DB.
    """
    global _PG_REACHABLE
    _PG_REACHABLE = False


def truncate_all_xenon_tables() -> None:
    """Single-statement TRUNCATE of all xenon.* + events.outbox tables.

    Behavior contract (must match the legacy per-test fixture):
      - PG unreachable → no-op (offline dev workflow stays usable)
      - CASCADE so dependents do not block
      - Connection error → mark offline, do not raise. Any other
        SQLAlchemyError (deadlock, missing table) re-raises: "offline" must
        never hide a real failure (INV-test-db-leak trigger 4a)
      - Single statement so the parser, planner, and lock acquisition run
        once instead of 26 times
    """
    if not is_pg_reachable():
        return
    stmt = f"TRUNCATE {', '.join(XENON_TABLES)} CASCADE"
    try:
        with get_session_engine().begin() as conn:
            conn.execute(text(stmt))
    except SQLAlchemyError as exc:
        if not _is_connection_error(exc):
            raise
        mark_offline()


def _is_connection_error(exc: SQLAlchemyError) -> bool:
    """True when PG could not be reached, not when a statement failed.

    Written for the psycopg engines these helpers use (session, admin); it is
    not claimed to classify asyncpg errors. Offline means:
      - SQLSTATE class 08 (connection exception) or 57P0x (shutdown, cannot
        connect now, database dropped);
      - an OperationalError with no SQLSTATE (psycopg client-side connect
        failure: refused, timeout, auth/database rejected at connect);
      - an InterfaceError whose message names a closed/refused/lost
        connection. Other InterfaceErrors ("cursor already closed") are
        client bugs and re-raise.
    A deadlock (40P01) is also an OperationalError, so the class alone is not
    enough.
    """
    if getattr(exc, "connection_invalidated", False):
        return True
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None)
    if sqlstate:
        return sqlstate.startswith(("08", "57P0"))
    if isinstance(exc, OperationalError):
        return True
    if isinstance(exc, InterfaceError):
        msg = str(orig if orig is not None else exc).lower()
        return "connection" in msg and any(w in msg for w in ("closed", "refused", "lost"))
    return False


# ---------------------------------------------------------------------------
# Phase 2 — per-test transactional rollback
# ---------------------------------------------------------------------------


class _BoundConnectionProxy:
    """Proxy returned from `_BoundEngine.connect()`.

    The whole point of Phase 2 is that the test owns a single Connection inside
    a BEGIN/ROLLBACK envelope; app code that calls `get_sync_engine().connect()`
    must reuse that same physical connection so its writes are visible to the
    test AND get rolled back at end-of-test.

    Two behavioral wrinkles vs a raw Connection:

    - `close()` and `__exit__` are no-ops. SQLAlchemy's `Connection.close()`
      returns the connection to the pool, which would detach our outer
      transaction. Ownership lives with the `pg_session` fixture, not the
      caller of `engine.connect()`.
    - `commit()` / `rollback()` operate on a SAVEPOINT, not the outer
      transaction. Released savepoints become visible to the rest of the
      test's transaction, and the outer ROLLBACK still discards everything at
      end-of-test. Without this, an app-side `conn.commit()` would end the
      test's outer transaction and our rollback would have nothing to do.

    `__getattr__` forwards everything else (execute, scalar, etc.) to the real
    connection unchanged.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn
        self._savepoint: Any = None

    def __enter__(self) -> "_BoundConnectionProxy":
        self._savepoint = self._conn.begin_nested()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._savepoint is not None and self._savepoint.is_active:
            self._savepoint.rollback()
        self._savepoint = None

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        return self._conn.execute(*args, **kwargs)

    def commit(self) -> None:
        if self._savepoint is not None and self._savepoint.is_active:
            self._savepoint.commit()
        self._savepoint = self._conn.begin_nested()

    def rollback(self) -> None:
        if self._savepoint is not None and self._savepoint.is_active:
            self._savepoint.rollback()
        self._savepoint = self._conn.begin_nested()

    def close(self) -> None:
        # Lifecycle owned by pg_session fixture.
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class _BoundBeginContext:
    """Context manager returned by `_BoundEngine.begin()`.

    Mirrors `Engine.begin()` semantics: yields a Connection, commits the
    savepoint on clean exit, rolls back on exception. App code typically writes
    `with engine.begin() as conn: conn.execute(...)`, so `__enter__` must
    return the bare Connection (not a transaction object).
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn
        self._savepoint: Any = None

    def __enter__(self) -> Connection:
        self._savepoint = self._conn.begin_nested()
        return self._conn

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._savepoint is None:
            return None
        if exc_type is None and self._savepoint.is_active:
            self._savepoint.commit()
        elif self._savepoint.is_active:
            self._savepoint.rollback()
        self._savepoint = None


class _BoundEngine:
    """Engine-shaped wrapper that hands out the test's connection.

    Monkeypatched onto `xenon.db.engine._sync_engine` by
    `app_engine_bound_to_test` so any app code that calls `get_sync_engine()`
    sees this object instead of a real Engine. Implements the small subset of
    the Engine surface that app code actually uses: `.connect()`, `.begin()`,
    `.dispose()`.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def connect(self) -> _BoundConnectionProxy:
        return _BoundConnectionProxy(self._conn)

    def begin(self) -> _BoundBeginContext:
        return _BoundBeginContext(self._conn)

    def dispose(self) -> None:
        return None


@pytest.fixture
def pg_session() -> Any:
    """Per-test connection inside a transaction that always rolls back.

    Phase 2 swaps the autouse TRUNCATE pre+post pattern for this. Every write
    a test performs goes into the WAL but never commits; the outer ROLLBACK
    discards everything at end-of-test in O(1). ~10x cheaper than TRUNCATE
    on 26 tables.
    """
    if not is_pg_reachable():
        pytest.skip(f"PG test DB unreachable at {sync_test_db_url()}")
    engine = get_session_engine()
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


@pytest.fixture
def app_engine_bound_to_test(pg_session: Connection, monkeypatch: Any) -> Connection:
    """Make `xenon.db.engine.get_sync_engine()` return a wrapper bound to
    `pg_session`'s connection.

    Required for any test that exercises a route or CLI that opens its own
    SQLAlchemy session — without this, the route writes to a different
    connection and the test's rollback misses them entirely.

    Yields the underlying connection (same object pg_session yields) so tests
    can assert visibility directly on it.
    """
    import xenon.db.engine as engine_mod

    monkeypatch.setattr(engine_mod, "_sync_engine", _BoundEngine(pg_session))
    return pg_session


# ---------------------------------------------------------------------------
# Phase 3 — per-worker DB clone (pytest-xdist multiplier)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _ensure_worker_db() -> Any:
    """Per-worker test-DB guard for pytest-xdist runs.

    Each xdist worker needs its own physical database (xenon_test_gwN) so
    workers don't contend on shared tables — Phase 2's per-test BEGIN/ROLLBACK
    is connection-scoped, not cross-worker.

    Two creation paths:
      1. **CI** pre-creates the per-worker DBs in the workflow yaml, serially,
         before pytest starts. This fixture then just verifies the DB exists.
      2. **Local**: when the role has CREATEDB, this fixture creates the
         missing worker DB from the unsuffixed test DB as template.

    A worker DB that cannot be created fails the worker loudly. There is no
    fallback to one shared DB: shared DBs deadlocked TRUNCATE, which used to
    flip the session "offline" and let tests write to the cached
    `DATABASE_URL` engine (INV-test-db-leak trigger 4b).
    """
    wid = _resolve_worker_id(None)
    if wid is not None:
        _prepare_worker_db(wid)
    yield


def _prepare_worker_db(wid: str) -> None:
    from sqlalchemy.engine.url import make_url

    target_url = make_url(sync_test_db_url(worker_id=wid))
    template_url = make_url(_without_worker_suffix(sync_test_db_url(worker_id=wid), wid))
    target_db = target_url.database
    template_db = template_url.database
    if not target_db or target_db == template_db:
        raise UnsafeTestDatabaseError(
            f"xdist worker {wid}: worker DB {target_db!r} is not distinct from the "
            f"template {template_db!r}; workers must not share one DB."
        )
    admin_engine = create_engine(
        target_url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 2},
    )
    try:
        try:
            conn_cm = admin_engine.connect()
        except SQLAlchemyError as exc:
            if _is_connection_error(exc):
                # PG unreachable: `is_pg_reachable()` on the worker URL decides
                # (offline). The worker never switches to the shared DB.
                return
            raise
        with conn_cm as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": target_db},
            ).scalar()
            if exists:
                return
            try:
                conn.execute(text(f'CREATE DATABASE "{target_db}" TEMPLATE "{template_db}"'))
            except SQLAlchemyError as exc:
                raise UnsafeTestDatabaseError(
                    f"xdist worker {wid}: cannot create worker DB {target_db!r} from "
                    f"template {template_db!r} ({exc.__class__.__name__}: {exc}). Workers "
                    "must not share one DB. Fix: pre-create it (CREATE DATABASE "
                    f'"{target_db}" TEMPLATE "{template_db}"), grant CREATEDB to the '
                    "test role, or run pytest without -n."
                ) from exc
    finally:
        admin_engine.dispose()
