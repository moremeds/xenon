"""Regression: pytest must never write to the developer's DATABASE_URL.

Incident (INV-test-db-leak, 2026-10): local pytest runs wrote fixture rows
(u1/a1, cid-W2, dev-probe/synthetic, DU0000000, ...) into prod ``core_dev``.
Mechanism: an import-time singleton (``uw_api_stats.stats``) cached a sync
engine bound to the ``.env`` DATABASE_URL during collection; the autouse
fixtures kept that cached engine whenever the test DB looked "offline"; and
"offline" was triggered by any TRUNCATE error or by a worker DB that could
not be created under xdist.

Guards pinned here:
  1. Root conftest rebinds DATABASE_URL to the test DB at import time and
     refuses to start when the test DB is the DATABASE_URL database, is
     ``core_dev``, or has no ``test`` in its name.
  2. Both autouse fixtures clear the cached app engines on every path,
     the offline path included.
  3. ``truncate_all_xenon_tables`` only treats connection errors as offline;
     an xdist worker DB that cannot be created fails loudly.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg
import psycopg.errors
import pytest
from sqlalchemy.exc import InterfaceError, OperationalError, ProgrammingError

import xenon._test_db as tdb

REPO_ROOT = Path(__file__).resolve().parents[2]

# Port 1 on loopback refuses immediately: the "prod" and "test" DBs of the
# probe subprocess are unreachable, which is the offline path that leaked.
_FAKE_PROD = "postgresql+asyncpg://u@127.0.0.1:1/xe_guard_probe_prod"
_FAKE_TEST = "postgresql+asyncpg://u@127.0.0.1:1/xe_guard_probe_test"


# ---------------------------------------------------------------------------
# 1. Session guard: bind_database_url_to_test_db
# ---------------------------------------------------------------------------


def _env(**kw: str) -> dict[str, str]:
    return dict(kw)


def test_guard_rebinds_database_url_to_test_db() -> None:
    env = _env(
        DATABASE_URL="postgresql+asyncpg://app:pw@db.example:5432/core_dev",
        DATABASE_URL_TEST="postgresql+asyncpg://u@127.0.0.1:5432/xenon_test",
    )
    bound = tdb.bind_database_url_to_test_db(env)
    assert bound == "postgresql+asyncpg://u@127.0.0.1:5432/xenon_test"
    assert env["DATABASE_URL"] == bound
    # Only the identity of the original DB is kept, never its credentials.
    assert env[tdb.DB_GUARD_ORIGINAL_ENV] == "db.example/core_dev"


def test_guard_allows_unset_database_url() -> None:
    """CI exports only DATABASE_URL_TEST."""
    env = _env(DATABASE_URL_TEST="postgresql+asyncpg://u@localhost:5432/xenon_test")
    assert tdb.bind_database_url_to_test_db(env).endswith("/xenon_test")
    assert env[tdb.DB_GUARD_ORIGINAL_ENV] == ""


def test_guard_refuses_test_db_equal_to_database_url() -> None:
    """Same host+dbname refuses even across driver, port and loopback alias."""
    env = _env(
        DATABASE_URL="postgresql+asyncpg://a@localhost:5432/core_test",
        DATABASE_URL_TEST="postgresql+psycopg://b@127.0.0.1:6543/core_test",
    )
    with pytest.raises(tdb.UnsafeTestDatabaseError, match="DATABASE_URL_TEST"):
        tdb.bind_database_url_to_test_db(env)
    assert env["DATABASE_URL"] == "postgresql+asyncpg://a@localhost:5432/core_test"


@pytest.mark.parametrize("dbname", ["core_dev", "xenon", "postgres"])
def test_guard_refuses_non_test_db_name(dbname: str) -> None:
    env = _env(DATABASE_URL_TEST=f"postgresql+asyncpg://u@127.0.0.1:5432/{dbname}")
    with pytest.raises(tdb.UnsafeTestDatabaseError, match="test"):
        tdb.bind_database_url_to_test_db(env)


@pytest.mark.parametrize(
    "query",
    [
        "dbname=core_dev",  # psycopg would connect to core_dev
        "host=127.0.0.1",  # overrides the URL host
        "hostaddr=10.0.0.1",
        "port=5433",
        "service=prod",
        "options=-c%20dbname%3Dcore_dev",
        "dsn=postgresql%3A%2F%2Fu%40db.example%2Fcore_dev",  # asyncpg dsn
        "target_session_attrs=any",  # any key outside the allowlist
    ],
)
def test_guard_refuses_routing_query_in_test_url(query: str) -> None:
    """Query params can re-route the connection away from the URL's
    host/dbname, so the guard cannot trust what it parsed."""
    env = _env(DATABASE_URL_TEST=f"postgresql+psycopg://u@localhost/xenon_test?{query}")
    with pytest.raises(tdb.UnsafeTestDatabaseError, match="routing"):
        tdb.bind_database_url_to_test_db(env)


def test_guard_refuses_test_url_rerouted_onto_database_url() -> None:
    """Counterexample from review: `elsewhere/core_test?host=127.0.0.1` looks
    different from `localhost/core_test` but connects to it."""
    env = _env(
        DATABASE_URL="postgresql+asyncpg://u@localhost/core_test",
        DATABASE_URL_TEST="postgresql+asyncpg://u@elsewhere/core_test?host=127.0.0.1",
    )
    with pytest.raises(tdb.UnsafeTestDatabaseError):
        tdb.bind_database_url_to_test_db(env)


def test_guard_refuses_database_url_with_routing_query() -> None:
    """An original DATABASE_URL with routing params cannot be compared."""
    env = _env(
        DATABASE_URL="postgresql+asyncpg://u@elsewhere/other?dbname=xenon_test&host=localhost",
        DATABASE_URL_TEST="postgresql+asyncpg://u@localhost/xenon_test",
    )
    with pytest.raises(tdb.UnsafeTestDatabaseError, match="DATABASE_URL"):
        tdb.bind_database_url_to_test_db(env)


def test_guard_refuses_database_url_dsn_alias() -> None:
    """Review counterexample: asyncpg resolves `?dsn=` to db.example/xenon_test,
    the test target, although the URL itself has no host."""
    env = _env(
        DATABASE_URL="postgresql+asyncpg:///xenon_test?dsn=postgresql%3A%2F%2Fu%3Apw%40db.example%2Fxenon_test",
        DATABASE_URL_TEST="postgresql+asyncpg://u:pw@db.example/xenon_test",
    )
    with pytest.raises(tdb.UnsafeTestDatabaseError, match="DATABASE_URL"):
        tdb.bind_database_url_to_test_db(env)


def test_guard_allows_allowlisted_query_keys() -> None:
    env = _env(
        DATABASE_URL="postgresql+asyncpg://u@db.example/core_dev?sslmode=require",
        DATABASE_URL_TEST="postgresql+psycopg://u@localhost/xenon_test?sslmode=disable&connect_timeout=5",
    )
    assert tdb.bind_database_url_to_test_db(env).endswith("/xenon_test?sslmode=disable&connect_timeout=5")


@pytest.mark.parametrize(
    ("original", "test_url"),
    [
        ("postgresql+asyncpg://u@localhost/core_dev", "postgresql+asyncpg:///xenon_test"),
        ("postgresql+asyncpg:///core_dev", "postgresql+asyncpg://u@localhost/xenon_test"),
        ("postgresql+asyncpg:///xenon_test", "postgresql+asyncpg:///xenon_test"),
    ],
)
def test_guard_refuses_empty_host_unless_both_and_distinct(original: str, test_url: str) -> None:
    """An empty host routes via socket/PGHOST, so it cannot be compared with a
    real host. Both empty: compare dbname only."""
    env = _env(DATABASE_URL=original, DATABASE_URL_TEST=test_url)
    with pytest.raises(tdb.UnsafeTestDatabaseError):
        tdb.bind_database_url_to_test_db(env)


def test_guard_allows_both_empty_hosts_with_distinct_dbnames() -> None:
    env = _env(DATABASE_URL="postgresql+asyncpg:///core_dev", DATABASE_URL_TEST="postgresql+asyncpg:///xenon_test")
    assert tdb.bind_database_url_to_test_db(env) == "postgresql+asyncpg:///xenon_test"


def test_guard_keeps_non_routing_query_and_suffixes_database() -> None:
    env = _env(
        DATABASE_URL_TEST="postgresql+asyncpg://u@localhost/xenon_test?application_name=pytest",
        PYTEST_XDIST_WORKER="gw0",
    )
    bound = tdb.bind_database_url_to_test_db(env)
    assert bound == "postgresql+asyncpg://u@localhost/xenon_test_gw0?application_name=pytest"


def test_worker_suffix_changes_database_not_query(monkeypatch) -> None:
    """Review counterexample: a raw-string split suffixed the query value
    (`application_name=pytest_gw0`) and left every worker on `xenon_test`."""
    monkeypatch.setenv("DATABASE_URL_TEST", "postgresql+asyncpg://u@localhost/xenon_test?application_name=pytest")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    url = tdb.sync_test_db_url()
    assert url == "postgresql+psycopg://u@localhost/xenon_test_gw0?application_name=pytest"
    assert tdb.sync_test_db_url() == url  # idempotent


def test_guard_in_xdist_worker_uses_inherited_original_and_worker_db() -> None:
    """xdist workers inherit the controller's rewritten DATABASE_URL. The guard
    must compare against the inherited original identity, not the rewritten
    value, and bind the worker to its own ``_gwN`` DB."""
    env = _env(
        DATABASE_URL="postgresql+asyncpg://u@127.0.0.1:5432/xenon_test",
        DATABASE_URL_TEST="postgresql+asyncpg://u@127.0.0.1:5432/xenon_test",
        PYTEST_XDIST_WORKER="gw2",
        **{tdb.DB_GUARD_ORIGINAL_ENV: "macmini/core_dev"},
    )
    assert tdb.bind_database_url_to_test_db(env).endswith("/xenon_test_gw2")
    assert env["DATABASE_URL"].endswith("/xenon_test_gw2")


def test_guard_in_xdist_worker_refuses_when_original_is_test_db() -> None:
    env = _env(
        DATABASE_URL="postgresql+asyncpg://u@127.0.0.1:5432/xenon_test",
        DATABASE_URL_TEST="postgresql+asyncpg://u@127.0.0.1:5432/xenon_test",
        PYTEST_XDIST_WORKER="gw0",
        **{tdb.DB_GUARD_ORIGINAL_ENV: "localhost/xenon_test"},
    )
    with pytest.raises(tdb.UnsafeTestDatabaseError):
        tdb.bind_database_url_to_test_db(env)


def test_session_is_bound_to_test_db() -> None:
    """The running session itself: the guard ran at root-conftest import."""
    assert tdb.DB_GUARD_ORIGINAL_ENV in os.environ


# ---------------------------------------------------------------------------
# Subprocess checks: real pytest sessions with a clean env
# ---------------------------------------------------------------------------


def _subprocess_env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_") and k != tdb.DB_GUARD_ORIGINAL_ENV}
    env.update(overrides)
    return env


def _run_pytest(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_session_refuses_when_test_db_equals_database_url() -> None:
    proc = _run_pytest(
        ["scripts/tests/test_worker_db_url.py"],
        _subprocess_env(DATABASE_URL=_FAKE_TEST, DATABASE_URL_TEST=_FAKE_TEST),
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "Refusing to run pytest" in out, out
    assert " passed" not in out, out


@pytest.mark.parametrize(
    "probe",
    ["scripts/tests/db_guard_probe.py", "src/xenon/api/tests/db_guard_probe.py"],
)
def test_offline_session_never_uses_database_url_engine(probe: str) -> None:
    """Probe session: DATABASE_URL=fake prod, test DB unreachable (offline).
    The probe asserts the import-time singleton bound to the test DB, and that
    a stale cached engine does not survive into a test (see the probe file)."""
    proc = _run_pytest(
        [probe],
        _subprocess_env(
            DATABASE_URL=_FAKE_PROD,
            DATABASE_URL_TEST=_FAKE_TEST,
            XENON_TEST_DB_GUARD_PROBE="1",
        ),
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "2 passed" in out, out


# ---------------------------------------------------------------------------
# 3. Offline classification
# ---------------------------------------------------------------------------


class _RaisingEngine:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def begin(self):
        raise self._exc

    def connect(self):
        raise self._exc


def test_truncate_reraises_non_connection_errors(monkeypatch) -> None:
    exc = OperationalError("TRUNCATE ...", {}, psycopg.errors.DeadlockDetected("deadlock detected"))
    monkeypatch.setattr(tdb, "_PG_REACHABLE", True)
    monkeypatch.setattr(tdb, "get_session_engine", lambda: _RaisingEngine(exc))
    with pytest.raises(OperationalError):
        tdb.truncate_all_xenon_tables()
    assert tdb._PG_REACHABLE is True


def test_truncate_reraises_cursor_interface_error(monkeypatch) -> None:
    """Review counterexample: an InterfaceError without SQLSTATE is a client
    lifecycle bug, not "offline", unless it names a lost connection."""
    exc = InterfaceError("TRUNCATE ...", {}, psycopg.InterfaceError("cursor already closed"))
    monkeypatch.setattr(tdb, "_PG_REACHABLE", True)
    monkeypatch.setattr(tdb, "get_session_engine", lambda: _RaisingEngine(exc))
    with pytest.raises(InterfaceError):
        tdb.truncate_all_xenon_tables()
    assert tdb._PG_REACHABLE is True


def test_truncate_marks_offline_on_closed_connection_interface_error(monkeypatch) -> None:
    exc = InterfaceError("TRUNCATE ...", {}, psycopg.InterfaceError("the connection is closed"))
    monkeypatch.setattr(tdb, "_PG_REACHABLE", True)
    monkeypatch.setattr(tdb, "get_session_engine", lambda: _RaisingEngine(exc))
    tdb.truncate_all_xenon_tables()
    assert tdb._PG_REACHABLE is False


def test_truncate_marks_offline_on_connection_error(monkeypatch) -> None:
    exc = OperationalError("TRUNCATE ...", {}, psycopg.OperationalError("connection refused"))
    monkeypatch.setattr(tdb, "_PG_REACHABLE", True)
    monkeypatch.setattr(tdb, "get_session_engine", lambda: _RaisingEngine(exc))
    tdb.truncate_all_xenon_tables()
    assert tdb._PG_REACHABLE is False


def test_reachability_probe_reraises_non_connection_errors(monkeypatch) -> None:
    """A probe failure that is not a connection error must surface instead of
    caching "offline" for the whole session."""
    exc = ProgrammingError("SELECT 1", {}, psycopg.errors.InsufficientPrivilege("permission denied"))
    monkeypatch.setattr(tdb, "_PG_REACHABLE", None)
    monkeypatch.setattr(tdb, "get_session_engine", lambda: _RaisingEngine(exc))
    with pytest.raises(ProgrammingError):
        tdb.is_pg_reachable()
    assert tdb._PG_REACHABLE is None


def test_reachability_probe_caches_offline_on_connection_error(monkeypatch) -> None:
    exc = OperationalError("connect", {}, psycopg.OperationalError("connection refused"))
    monkeypatch.setattr(tdb, "_PG_REACHABLE", None)
    monkeypatch.setattr(tdb, "get_session_engine", lambda: _RaisingEngine(exc))
    assert tdb.is_pg_reachable() is False
    assert tdb._PG_REACHABLE is False


class _FakeAdminConn:
    def __init__(self, create_exc: Exception | None, statements: list[str]) -> None:
        self._create_exc = create_exc
        self._statements = statements

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self._statements.append(sql)
        if sql.startswith("CREATE DATABASE") and self._create_exc is not None:
            raise self._create_exc

        class _R:
            def scalar(self_inner):
                return None  # worker DB does not exist yet

        return _R()


class _FakeAdminEngine:
    def __init__(self, conn: _FakeAdminConn | None, connect_exc: Exception | None = None) -> None:
        self._conn = conn
        self._connect_exc = connect_exc

    def connect(self):
        if self._connect_exc is not None:
            raise self._connect_exc
        return self._conn

    def dispose(self) -> None:
        return None


def _patch_admin(monkeypatch, engine: _FakeAdminEngine) -> None:
    monkeypatch.setenv("DATABASE_URL_TEST", "postgresql+asyncpg://u@127.0.0.1:5432/xenon_test")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    monkeypatch.setattr(tdb, "create_engine", lambda *a, **k: engine)


def test_worker_db_created_from_unsuffixed_template(monkeypatch) -> None:
    statements: list[str] = []
    _patch_admin(monkeypatch, _FakeAdminEngine(_FakeAdminConn(None, statements)))
    tdb._prepare_worker_db("gw3")
    create = [s for s in statements if s.startswith("CREATE DATABASE")]
    assert create == ['CREATE DATABASE "xenon_test_gw3" TEMPLATE "xenon_test"']


def test_worker_db_with_query_url_is_not_the_template(monkeypatch) -> None:
    statements: list[str] = []
    _patch_admin(monkeypatch, _FakeAdminEngine(_FakeAdminConn(None, statements)))
    monkeypatch.setenv("DATABASE_URL_TEST", "postgresql+asyncpg://u@127.0.0.1:5432/xenon_test?application_name=pytest")
    tdb._prepare_worker_db("gw3")
    create = [s for s in statements if s.startswith("CREATE DATABASE")]
    assert create == ['CREATE DATABASE "xenon_test_gw3" TEMPLATE "xenon_test"']


def test_worker_db_equal_to_template_fails_loudly(monkeypatch) -> None:
    """Defense in depth: if suffixing ever leaves the worker DB equal to the
    template, the worker must fail instead of sharing the template DB."""
    _patch_admin(monkeypatch, _FakeAdminEngine(_FakeAdminConn(None, [])))
    monkeypatch.setattr(tdb, "_with_worker_suffix", lambda url, wid: url)
    with pytest.raises(tdb.UnsafeTestDatabaseError, match="not distinct"):
        tdb._prepare_worker_db("gw3")


def test_worker_db_create_failure_fails_loudly(monkeypatch) -> None:
    denied = ProgrammingError(
        "CREATE DATABASE", {}, psycopg.errors.InsufficientPrivilege("permission denied to create database")
    )
    _patch_admin(monkeypatch, _FakeAdminEngine(_FakeAdminConn(denied, [])))
    with pytest.raises(tdb.UnsafeTestDatabaseError, match="xenon_test_gw3"):
        tdb._prepare_worker_db("gw3")
    # No silent fallback: the worker keeps its own DB name.
    assert tdb.sync_test_db_url().endswith("/xenon_test_gw3")


def test_worker_db_admin_unreachable_is_offline_not_shared(monkeypatch) -> None:
    refused = OperationalError("connect", {}, psycopg.OperationalError("connection refused"))
    _patch_admin(monkeypatch, _FakeAdminEngine(None, connect_exc=refused))
    tdb._prepare_worker_db("gw3")
    assert tdb.sync_test_db_url().endswith("/xenon_test_gw3")
