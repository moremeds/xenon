"""Verify the scoped-test fixtures exist and behave per the PG migration plan.

These fixtures are the prerequisite layer for the clean-cutoff PG migration
(`docs/plans/2026-05-03-pg-migration-clean-cutoff.md`). Every migrated CLI calls
`AccountScope.resolve_from_env()`, which raises if `XENON_BROKER_ACCOUNT` is
unset — so the autouse env fixture must export it for every test.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import Engine

import scripts.tests.conftest as shared_conftest
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
    """A reachable but unmigrated test DB should not fail unrelated tests."""

    class FakeConnection:
        def execute(self, _statement):
            raise SQLAlchemyError("missing schema")

    class FakeBegin:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, *_exc_info):
            return False

    class FakeEngine:
        disposed = False

        def begin(self):
            return FakeBegin()

        def dispose(self):
            self.disposed = True

    fake_engine = FakeEngine()
    monkeypatch.setattr(shared_conftest, "_PG_REACHABLE_CACHE", True)
    monkeypatch.setattr(shared_conftest, "create_engine", lambda *_args, **_kwargs: fake_engine)

    shared_conftest._truncate_postgres_tables()

    assert shared_conftest._PG_REACHABLE_CACHE is False
    assert fake_engine.disposed is True
