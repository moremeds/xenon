"""Probe session for `test_test_db_guard.py` (not collected by default).

Runs only in a subprocess pytest session started by
`test_offline_session_never_uses_database_url_engine`, with DATABASE_URL set
to a fake "prod" DB and an unreachable test DB (offline path).
"""

from __future__ import annotations

import os

import pytest

if os.environ.get("XENON_TEST_DB_GUARD_PROBE") != "1":
    pytest.skip("probe module: run only by test_test_db_guard.py", allow_module_level=True)

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine

import xenon.db.engine as engine_mod
import xenon.utils.uw_api_stats  # noqa: F401 — import-time singleton calls get_sync_engine()

# Engine the import-time singleton cached during collection.
_IMPORT_TIME_DB = engine_mod._sync_engine.url.database if engine_mod._sync_engine is not None else None

# Simulate a stale engine cached before a test (as a lifespan or an earlier
# module import would leave it). It must never be visible inside a test.
engine_mod._sync_engine = create_engine("postgresql+psycopg://u@127.0.0.1:1/xe_guard_probe_stale")
engine_mod._engine = create_async_engine("postgresql+asyncpg://u@127.0.0.1:1/xe_guard_probe_stale")


def test_probe_import_time_singleton_bound_to_test_db():
    assert _IMPORT_TIME_DB == "xe_guard_probe_test"


def test_probe_cached_engine_never_survives_into_test():
    assert engine_mod._sync_engine is None
    # Async engine: cleared (scripts conftest) or re-initialised on the test
    # DB (api conftest) — never the stale one.
    assert engine_mod._engine is None or engine_mod._engine.url.database == "xe_guard_probe_test"
    assert engine_mod.get_sync_engine().url.database == "xe_guard_probe_test"
