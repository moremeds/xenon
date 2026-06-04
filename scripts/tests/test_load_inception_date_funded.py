"""load_inception_date must return the earliest *funded* date (NAV > 0).

Live data showed IB accounts with ~2 months of zero-NAV rows before the
first deposit landed. Treating the account-open date as "inception" made
inception-to-date TWR divide by zero (NAV[0]=0 in the denominator), which
surfaced in the UI as +1507% — a math artifact, not real performance.

Fix: filter on ``nav > 0``. This trims leading unfunded rows at the
source so both ``resolve_period_start("All", ...)`` and the displayed
inception string land on the right day.

Seeds via the sync ``pg_test_engine`` fixture (works locally) and exercises
``load_inception_date`` via a freshly-built async engine + ``asyncio.run``.
The repo's standing ``async_engine`` fixture currently skips on this
worktree (see existing test_nav_history_queries.py — pre-existing infra
gap, tracked elsewhere). Going through a fresh engine sidesteps that.
"""

from __future__ import annotations

import asyncio
import os
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from xenon.db.queries.nav_history import load_inception_date
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DUQ_INCEPT")


def _async_url() -> str:
    url = os.environ.get("DATABASE_URL_TEST") or os.environ.get("DATABASE_URL") or ""
    if "postgresql+psycopg://" in url:
        return url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _seed(pg_test_engine, day: date, nav: float) -> None:
    with pg_test_engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO xenon.nav_history "
                "(broker, account_env, broker_account, date, nav, source) "
                "VALUES (:b, :e, :a, :d, :n, 'close') "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "b": SCOPE.broker,
                "e": SCOPE.account_env,
                "a": SCOPE.broker_account,
                "d": day,
                "n": nav,
            },
        )


def _purge(pg_test_engine) -> None:
    with pg_test_engine.begin() as c:
        c.execute(
            text("DELETE FROM xenon.nav_history WHERE broker_account = :a"),
            {"a": SCOPE.broker_account},
        )


async def _call_with_fresh_engine():
    eng = create_async_engine(_async_url(), pool_pre_ping=True)
    try:
        return await load_inception_date(eng, SCOPE)
    finally:
        await eng.dispose()


def test_load_inception_date_skips_leading_zero_nav(pg_test_engine):
    """Three rows pre-deposit (NAV=0), then funded rows.
    Inception must be the funded day, not the account-open day."""
    _purge(pg_test_engine)
    _seed(pg_test_engine, date(2025, 9, 1), 0)
    _seed(pg_test_engine, date(2025, 9, 15), 0)
    _seed(pg_test_engine, date(2025, 10, 1), 0)
    _seed(pg_test_engine, date(2025, 10, 2), 1000)
    _seed(pg_test_engine, date(2025, 10, 3), 1010)
    try:
        result = asyncio.run(_call_with_fresh_engine())
    finally:
        _purge(pg_test_engine)
    assert result == date(2025, 10, 2), f"expected first funded date 2025-10-02, got {result}"


def test_load_inception_date_none_when_no_funded_rows(pg_test_engine):
    """Account that opened but never received a deposit → None."""
    _purge(pg_test_engine)
    _seed(pg_test_engine, date(2025, 9, 1), 0)
    _seed(pg_test_engine, date(2025, 9, 2), 0)
    try:
        result = asyncio.run(_call_with_fresh_engine())
    finally:
        _purge(pg_test_engine)
    assert result is None, f"expected None for unfunded account, got {result}"


def test_load_inception_date_none_when_scope_absent(pg_test_engine):
    """No rows for the scope at all → None (existing behavior preserved)."""
    _purge(pg_test_engine)
    result = asyncio.run(_call_with_fresh_engine())
    assert result is None


def test_load_inception_date_returns_first_row_when_all_funded(pg_test_engine):
    """No leading zero rows → returns the actual first row (no behavior change)."""
    _purge(pg_test_engine)
    _seed(pg_test_engine, date(2025, 11, 5), 5000)
    _seed(pg_test_engine, date(2025, 11, 6), 5100)
    try:
        result = asyncio.run(_call_with_fresh_engine())
    finally:
        _purge(pg_test_engine)
    assert result == date(2025, 11, 5)
