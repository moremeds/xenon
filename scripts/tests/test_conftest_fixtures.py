"""Verify the scoped-test fixtures exist and behave per the PG migration plan.

These fixtures are the prerequisite layer for the clean-cutoff PG migration
(`docs/plans/2026-05-03-pg-migration-clean-cutoff.md`). Every migrated CLI calls
`AccountScope.resolve_from_env()`, which raises if `XENON_BROKER_ACCOUNT` is
unset — so the autouse env fixture must export it for every test.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

import xenon._test_db as db_fixture
from xenon.execution.account_scope import AccountScope, resolve_from_env


def test_xenon_broker_account_env_set_for_every_test():
    assert os.environ.get("XENON_BROKER_ACCOUNT") == "DU0000000"


def test_xenon_trading_mode_env_set_for_every_test():
    assert os.environ.get("XENON_TRADING_MODE") == "paper"


def test_resolve_from_env_returns_paper_scope_in_test_context():
    scope = resolve_from_env()
    assert scope.broker == "IB"
    assert scope.account_env == "paper"
    assert scope.broker_account == "DU0000000"


def test_scope_fixture_yields_paper_ib_scope(scope_fixture):
    assert isinstance(scope_fixture, AccountScope)
    assert scope_fixture.broker == "IB"
    assert scope_fixture.account_env == "paper"
    assert scope_fixture.broker_account == "DU0000000"


def test_pg_test_engine_is_sync_engine(pg_test_engine):
    assert isinstance(pg_test_engine, Engine)
    url = str(pg_test_engine.url)
    assert "psycopg" in url, f"pg_test_engine must be sync (psycopg), got {url}"
    assert "asyncpg" not in url, f"pg_test_engine must not be async, got {url}"


def test_pg_test_engine_points_at_test_db(pg_test_engine):
    db_name = pg_test_engine.url.database or ""
    assert "test" in db_name.lower(), (
        f"pg_test_engine must point at a *_test database (defense in depth), got {db_name!r}"
    )


def test_truncate_postgres_tables_treats_missing_schema_as_offline(monkeypatch):
    """A reachable but unmigrated test DB should not fail unrelated tests.

    Contract guarded here: when TRUNCATE fails with SQLAlchemyError (the
    typical symptom of a missing schema), the helper must swallow it and
    flip the reachability cache to False so subsequent tests skip TRUNCATE.
    """

    class FakeConnection:
        def execute(self, _statement):
            raise SQLAlchemyError("missing schema")

    class FakeBegin:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, *_exc_info):
            return False

    class FakeEngine:
        def begin(self):
            return FakeBegin()

    # Skip the reachability probe (pretend the DB is up) and inject our fake
    # session engine so TRUNCATE attempts hit it.
    monkeypatch.setattr(db_fixture, "_PG_REACHABLE", True)
    monkeypatch.setattr(db_fixture, "_SESSION_ENGINE", FakeEngine())

    db_fixture.truncate_all_xenon_tables()

    assert db_fixture._PG_REACHABLE is False
