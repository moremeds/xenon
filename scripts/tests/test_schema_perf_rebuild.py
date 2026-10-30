"""Schema tests for Phase 1 of performance rebuild.

Covers: benchmark_closes table, nav_history.nav_type column,
nav_history_one_env_per_day unique index.

Spec: docs/superpowers/specs/2026-05-31-performance-rebuild-design.md

History: the column was originally added as `source` (migration
260fabba18d6) with values ('close','intraday'). The 2026_06_01_0001
migration renamed it to `nav_type` and broadened the constraint to
('close','intraday','reported','reconstructed') to accommodate the
Futu backward-walk reconstruction state machine.
"""

from datetime import date

import pytest
import sqlalchemy as sa

from xenon.db.schema import benchmark_closes, nav_history

# ---------- Migration 1: benchmark_closes ----------


def test_benchmark_closes_insert_and_read(pg_test_engine):
    with pg_test_engine.begin() as conn:
        conn.execute(sa.delete(benchmark_closes).where(benchmark_closes.c.symbol == "_TEST_SPY_"))
        conn.execute(sa.insert(benchmark_closes).values(symbol="_TEST_SPY_", date=date(2026, 6, 1), close="450.00"))
        row = conn.execute(sa.select(benchmark_closes).where(benchmark_closes.c.symbol == "_TEST_SPY_")).first()
        # cleanup
        conn.execute(sa.delete(benchmark_closes).where(benchmark_closes.c.symbol == "_TEST_SPY_"))
    assert row is not None
    assert float(row.close) == 450.00


# ---------- Migration 2: nav_history.nav_type ----------


def _seed_nav_row(conn, *, broker, account_env, broker_account, date_, nav, nav_type=None):
    values = dict(
        broker=broker,
        account_env=account_env,
        broker_account=broker_account,
        date=date_,
        nav=nav,
        daily_pnl="0.00",
    )
    if nav_type is not None:
        values["nav_type"] = nav_type
    conn.execute(sa.insert(nav_history).values(**values))


def test_nav_history_nav_type_defaults_to_intraday(pg_test_engine):
    with pg_test_engine.begin() as conn:
        conn.execute(sa.delete(nav_history).where(nav_history.c.broker_account == "_TEST_SRC_"))
        _seed_nav_row(
            conn,
            broker="IB",
            account_env="paper",
            broker_account="_TEST_SRC_",
            date_=date(2026, 6, 1),
            nav="50000.00",
        )
        row = conn.execute(
            sa.select(nav_history.c.nav_type).where(nav_history.c.broker_account == "_TEST_SRC_")
        ).first()
        conn.execute(sa.delete(nav_history).where(nav_history.c.broker_account == "_TEST_SRC_"))
    assert row.nav_type == "intraday"


def test_nav_history_nav_type_rejects_unknown_value(pg_test_engine):
    with pytest.raises(sa.exc.IntegrityError):
        with pg_test_engine.begin() as conn:
            _seed_nav_row(
                conn,
                broker="IB",
                account_env="paper",
                broker_account="_TEST_SRC_BAD_",
                date_=date(2026, 6, 2),
                nav="50000.00",
                nav_type="bogus",
            )


# ---------- Migration 3: unique index ----------


def test_two_different_account_envs_for_same_account_date_rejected(pg_test_engine):
    """Decisions §13 — partial unique index makes dual-curve writes impossible."""
    with pg_test_engine.begin() as conn:
        conn.execute(sa.delete(nav_history).where(nav_history.c.broker_account == "_TEST_UIX_"))
        _seed_nav_row(
            conn,
            broker="FUTU",
            account_env="live",
            broker_account="_TEST_UIX_",
            date_=date(2026, 6, 1),
            nav="100000.00",
        )
    try:
        with pytest.raises(sa.exc.IntegrityError):
            with pg_test_engine.begin() as conn:
                _seed_nav_row(
                    conn,
                    broker="FUTU",
                    account_env="paper",
                    broker_account="_TEST_UIX_",
                    date_=date(2026, 6, 1),
                    nav="200000.00",
                )
    finally:
        with pg_test_engine.begin() as conn:
            conn.execute(sa.delete(nav_history).where(nav_history.c.broker_account == "_TEST_UIX_"))


def test_existing_PK_still_blocks_same_env_dup(pg_test_engine):
    """Sanity: unique index does not relax the PK."""
    with pg_test_engine.begin() as conn:
        conn.execute(sa.delete(nav_history).where(nav_history.c.broker_account == "_TEST_PKD_"))
        _seed_nav_row(
            conn,
            broker="FUTU",
            account_env="live",
            broker_account="_TEST_PKD_",
            date_=date(2026, 6, 1),
            nav="100000.00",
        )
    try:
        with pytest.raises(sa.exc.IntegrityError):
            with pg_test_engine.begin() as conn:
                _seed_nav_row(
                    conn,
                    broker="FUTU",
                    account_env="live",
                    broker_account="_TEST_PKD_",
                    date_=date(2026, 6, 1),
                    nav="200000.00",
                )
    finally:
        with pg_test_engine.begin() as conn:
            conn.execute(sa.delete(nav_history).where(nav_history.c.broker_account == "_TEST_PKD_"))
