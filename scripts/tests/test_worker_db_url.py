"""Phase 3 regression: sync_test_db_url is worker-aware.

Each pytest-xdist worker runs in its own process and must see a distinct test
database name to avoid contending on the same WAL / lock table. The autouse
session fixture in `_test_db.py` clones `xenon_test` per worker; this test
asserts the URL helper rewrites cleanly so the clone target and the engine URL
stay in sync.
"""

from __future__ import annotations

import pytest

# Pure unit tests on a pure function — no DB I/O, no need for the txn-rollback
# autouse machinery. But the marker is harmless and keeps consistency with the
# rest of the helper-touching tests in this tree.
pytestmark = pytest.mark.committed_db

from xenon._test_db import sync_test_db_url


def test_master_worker_uses_unsuffixed_db(monkeypatch):
    """Serial pytest (no xdist) → DB name unchanged."""
    monkeypatch.setenv(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://u:p@h:5432/xenon_test",
    )
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    url = sync_test_db_url()
    assert url == "postgresql+psycopg://u:p@h:5432/xenon_test"


def test_xdist_worker_gets_suffixed_db(monkeypatch):
    """Worker id from env → /xenon_test_gw0."""
    monkeypatch.setenv(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://u:p@h:5432/xenon_test",
    )
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    url = sync_test_db_url()
    assert url == "postgresql+psycopg://u:p@h:5432/xenon_test_gw0"


def test_master_worker_id_is_treated_as_serial(monkeypatch):
    """PYTEST_XDIST_WORKER=master (xdist's own sentinel) → no rewrite."""
    monkeypatch.setenv(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://u:p@h:5432/xenon_test",
    )
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "master")
    url = sync_test_db_url()
    assert url.endswith("/xenon_test")


def test_explicit_worker_id_overrides_env(monkeypatch):
    """Caller-supplied worker_id wins over env (used by ensure-worker-db fixture)."""
    monkeypatch.setenv(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://u:p@h:5432/xenon_test",
    )
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    url = sync_test_db_url(worker_id="gw2")
    assert url.endswith("/xenon_test_gw2")


def test_suffix_rewrite_is_idempotent(monkeypatch):
    """Calling sync_test_db_url after the autouse fixture has rewritten
    DATABASE_URL_TEST in env must NOT append a second `_gwN` suffix.

    The autouse fixture sets DATABASE_URL_TEST to the worker-suffixed URL
    so test helpers reading the env directly land on the right DB. The next
    call to sync_test_db_url() reads that already-suffixed env value — the
    function must detect and skip the second rewrite.
    """
    monkeypatch.setenv(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://u:p@h:5432/xenon_test_gw0",
    )
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    url = sync_test_db_url()
    assert url == "postgresql+psycopg://u:p@h:5432/xenon_test_gw0"
    # Calling again must return the same thing — not xenon_test_gw0_gw0.
    assert sync_test_db_url() == url


def test_async_url_matches_sync_url_db_name(monkeypatch):
    """async_test_db_url must point at the same DB as sync_test_db_url —
    a mismatch would cause the FastAPI route engine and the test's
    BEGIN/ROLLBACK connection to write to different physical databases.
    """
    from xenon._test_db import async_test_db_url

    monkeypatch.setenv(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://u:p@h:5432/xenon_test",
    )
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    sync_url = sync_test_db_url()
    async_url = async_test_db_url()
    assert sync_url.replace("postgresql+psycopg://", "") == async_url.replace("postgresql+asyncpg://", "")
    assert async_url.endswith("/xenon_test_gw1")
