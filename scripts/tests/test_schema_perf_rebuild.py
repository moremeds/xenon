"""Schema tests for Phase 1 of performance rebuild.

Covers: benchmark_closes table, nav_history.source column,
nav_history_one_env_per_day unique index.

Spec: docs/superpowers/specs/2026-05-31-performance-rebuild-design.md
"""
from datetime import date

import pytest
# Phase 2 carve-out: this module's tests open their own SQLAlchemy engine
# (helpers calling sqlalchemy.create_engine directly, or subprocess CLIs)
# and therefore can't share the test's BEGIN/ROLLBACK transaction. They
# stay on Phase 1 TRUNCATE pre+post isolation via this marker. Migration
# to txn-rollback would require refactoring those local engine helpers to
# go through xenon.db.engine.get_sync_engine().
pytestmark = pytest.mark.committed_db

import sqlalchemy as sa
from xenon.db.schema import benchmark_closes, nav_history


# ---------- Migration 1: benchmark_closes ----------


def test_benchmark_closes_insert_and_read(pg_test_engine):
    with pg_test_engine.begin() as conn:
        conn.execute(sa.delete(benchmark_closes).where(benchmark_closes.c.symbol == "_TEST_SPY_"))
        conn.execute(
            sa.insert(benchmark_closes).values(
                symbol="_TEST_SPY_", date=date(2026, 6, 1), close="450.00"
            )
        )
        row = conn.execute(
            sa.select(benchmark_closes).where(benchmark_closes.c.symbol == "_TEST_SPY_")
        ).first()
        # cleanup
        conn.execute(sa.delete(benchmark_closes).where(benchmark_closes.c.symbol == "_TEST_SPY_"))
    assert row is not None
    assert float(row.close) == 450.00


# ---------- Migration 2: nav_history.source ----------


def _seed_nav_row(conn, *, broker, account_env, broker_account, date_, nav, source=None):
    values = dict(
        broker=broker, account_env=account_env, broker_account=broker_account,
        date=date_, nav=nav, daily_pnl="0.00",
    )
    if source is not None:
        values["source"] = source
    conn.execute(sa.insert(nav_history).values(**values))


def test_nav_history_source_defaults_to_intraday(pg_test_engine):
    with pg_test_engine.begin() as conn:
        conn.execute(sa.delete(nav_history).where(nav_history.c.broker_account == "_TEST_SRC_"))
        _seed_nav_row(
            conn, broker="IB", account_env="paper", broker_account="_TEST_SRC_",
            date_=date(2026, 6, 1), nav="50000.00",
        )
        row = conn.execute(
            sa.select(nav_history.c.source).where(nav_history.c.broker_account == "_TEST_SRC_")
        ).first()
        conn.execute(sa.delete(nav_history).where(nav_history.c.broker_account == "_TEST_SRC_"))
    assert row.source == "intraday"


def test_nav_history_source_rejects_unknown_value(pg_test_engine):
    with pytest.raises(sa.exc.IntegrityError):
        with pg_test_engine.begin() as conn:
            _seed_nav_row(
                conn, broker="IB", account_env="paper", broker_account="_TEST_SRC_BAD_",
                date_=date(2026, 6, 2), nav="50000.00", source="bogus",
            )


# ---------- Migration 3: unique index ----------


def test_two_different_account_envs_for_same_account_date_rejected(pg_test_engine):
    """Decisions §13 — partial unique index makes dual-curve writes impossible."""
    with pg_test_engine.begin() as conn:
        conn.execute(sa.delete(nav_history).where(nav_history.c.broker_account == "_TEST_UIX_"))
        _seed_nav_row(
            conn, broker="FUTU", account_env="live", broker_account="_TEST_UIX_",
            date_=date(2026, 6, 1), nav="100000.00",
        )
    try:
        with pytest.raises(sa.exc.IntegrityError):
            with pg_test_engine.begin() as conn:
                _seed_nav_row(
                    conn, broker="FUTU", account_env="paper", broker_account="_TEST_UIX_",
                    date_=date(2026, 6, 1), nav="200000.00",
                )
    finally:
        with pg_test_engine.begin() as conn:
            conn.execute(sa.delete(nav_history).where(nav_history.c.broker_account == "_TEST_UIX_"))


def test_existing_PK_still_blocks_same_env_dup(pg_test_engine):
    """Sanity: unique index does not relax the PK."""
    with pg_test_engine.begin() as conn:
        conn.execute(sa.delete(nav_history).where(nav_history.c.broker_account == "_TEST_PKD_"))
        _seed_nav_row(
            conn, broker="FUTU", account_env="live", broker_account="_TEST_PKD_",
            date_=date(2026, 6, 1), nav="100000.00",
        )
    try:
        with pytest.raises(sa.exc.IntegrityError):
            with pg_test_engine.begin() as conn:
                _seed_nav_row(
                    conn, broker="FUTU", account_env="live", broker_account="_TEST_PKD_",
                    date_=date(2026, 6, 1), nav="200000.00",
                )
    finally:
        with pg_test_engine.begin() as conn:
            conn.execute(sa.delete(nav_history).where(nav_history.c.broker_account == "_TEST_PKD_"))
