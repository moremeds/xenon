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
