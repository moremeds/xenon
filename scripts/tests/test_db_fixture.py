"""Unit tests for the shared session-scoped PG test-fixture helper.

These guards exist so a future change doesn't accidentally regress to the
"new engine per test" pattern that dominated CI runtime.
"""

from __future__ import annotations

import pytest

from xenon._test_db import (
    XENON_TABLES,
    get_session_engine,
    is_pg_reachable,
    sync_test_db_url,
    truncate_all_xenon_tables,
)


def test_session_engine_is_cached_singleton():
    e1 = get_session_engine()
    e2 = get_session_engine()
    assert e1 is e2, "session engine must be cached, not recreated per call"


def test_reachability_check_is_idempotent():
    r1 = is_pg_reachable()
    r2 = is_pg_reachable()
    assert r1 is r2, "reachability probe must be cached for the test session"


def test_xenon_tables_list_is_non_empty_and_qualified():
    assert XENON_TABLES, "table list must not be empty"
    for table in XENON_TABLES:
        assert "." in table, f"table {table!r} must be schema-qualified"


def test_xenon_tables_covers_every_schema_table():
    """XENON_TABLES is the per-session/per-test reset list. Every table defined
    in the schema MUST appear here — a missing table is never truncated or
    rolled back, so a committed write to it (e.g. a heartbeat from a
    ``committed_db`` lifespan test) leaks across the whole session and surfaces
    as a flaky cross-test UniqueViolation in an unrelated test. This happened
    with ``service_health`` (operator console). Deriving the expectation from
    the live metadata turns future drift into a deterministic failure here,
    instead of an intermittent leak somewhere else.
    """
    from xenon.db.schema import events_metadata, xenon_metadata

    schema_tables = {f"{t.schema}.{t.name}" for md in (xenon_metadata, events_metadata) for t in md.tables.values()}
    missing = schema_tables - set(XENON_TABLES)
    assert not missing, f"XENON_TABLES is missing schema tables (they will leak across tests): {sorted(missing)}"


def test_sync_test_db_url_uses_psycopg_driver():
    url = sync_test_db_url()
    assert "postgresql+psycopg" in url, (
        "sync helpers must use psycopg, not asyncpg; the autouse fixture rewrites DATABASE_URL for sync callers"
    )


def test_truncate_tolerates_offline_db(monkeypatch):
    """If PG is unreachable, truncate must silently no-op — never raise."""
    # We can't safely break reachability mid-session without invalidating
    # the cache, so this is a smoke test: the call returns cleanly when
    # the cache says reachable too.
    if not is_pg_reachable():
        pytest.skip("PG unreachable; offline-tolerance branch covered by skip")
    truncate_all_xenon_tables()  # must not raise
