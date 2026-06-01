"""Tests for nav_history + benchmark cache query helpers."""
from datetime import date

import pytest
import sqlalchemy as sa

from xenon.db.queries.nav_history import (
    BenchmarkUnavailable,
    fetch_and_cache_benchmark,
    load_benchmark_cached,
    load_nav_curve,
)
from xenon.db.schema import benchmark_closes, nav_history
from xenon.execution.account_scope import AccountScope

pytestmark = pytest.mark.asyncio


# ---------- load_nav_curve ----------


async def _seed(conn, broker, env, account, d, nav):
    await conn.execute(
        sa.insert(nav_history).values(
            broker=broker, account_env=env, broker_account=account,
            date=d, nav=str(nav), daily_pnl=str(round(nav * 0.001, 2)),
            source="intraday",
        )
    )


async def _purge_nav(engine, account):
    async with engine.begin() as c:
        await c.execute(sa.delete(nav_history).where(nav_history.c.broker_account == account))


async def test_load_nav_curve_scope_isolation(async_engine):
    """Two distinct accounts (different broker_account) must not bleed
    into each other's curves. Same (broker, broker_account, date) with
    different env is impossible by design (unique index) so we use
    different broker_accounts to verify scope filtering."""
    await _purge_nav(async_engine, "T_NAV_SCOPE_A")
    await _purge_nav(async_engine, "T_NAV_SCOPE_B")
    async with async_engine.begin() as c:
        await _seed(c, "IB", "paper", "T_NAV_SCOPE_A", date(2026, 1, 1), 100)
        await _seed(c, "IB", "paper", "T_NAV_SCOPE_A", date(2026, 1, 2), 101)
        await _seed(c, "IB", "live", "T_NAV_SCOPE_B", date(2026, 1, 2), 999)
    df = await load_nav_curve(
        async_engine,
        AccountScope(broker="IB", account_env="paper", broker_account="T_NAV_SCOPE_A"),
        date(2026, 1, 1),
    )
    await _purge_nav(async_engine, "T_NAV_SCOPE_A")
    await _purge_nav(async_engine, "T_NAV_SCOPE_B")
    assert list(df["nav"]) == [100.0, 101.0]


async def test_load_nav_curve_period_filter(async_engine):
    await _purge_nav(async_engine, "T_NAV_PERIOD")
    async with async_engine.begin() as c:
        await _seed(c, "IB", "paper", "T_NAV_PERIOD", date(2025, 12, 31), 90)
        await _seed(c, "IB", "paper", "T_NAV_PERIOD", date(2026, 1, 1), 100)
    df = await load_nav_curve(
        async_engine,
        AccountScope(broker="IB", account_env="paper", broker_account="T_NAV_PERIOD"),
        date(2026, 1, 1),
    )
    await _purge_nav(async_engine, "T_NAV_PERIOD")
    assert len(df) == 1
    assert float(df["nav"].iloc[0]) == 100.0


async def test_load_nav_curve_empty_when_no_match(async_engine):
    df = await load_nav_curve(
        async_engine,
        AccountScope(broker="IB", account_env="paper", broker_account="NEVER_EXISTS"),
        date(2026, 1, 1),
    )
    assert df.empty


# ---------- load_benchmark_cached ----------


async def _purge_bench(engine, symbol):
    async with engine.begin() as c:
        await c.execute(sa.delete(benchmark_closes).where(benchmark_closes.c.symbol == symbol))


async def test_load_benchmark_cached_hit_no_fetch(async_engine):
    await _purge_bench(async_engine, "_T_HIT_")
    async with async_engine.begin() as c:
        await c.execute(
            sa.insert(benchmark_closes).values(
                symbol="_T_HIT_", date=date(2026, 6, 1), close="450.00"
            )
        )
    # No ib_pool — but cache is populated, so no fetch is attempted.
    df, err = await load_benchmark_cached(async_engine, None, "_T_HIT_", date(2026, 5, 1))
    await _purge_bench(async_engine, "_T_HIT_")
    assert err is None
    assert len(df) == 1
    assert float(df["close"].iloc[0]) == 450.00


async def test_load_benchmark_cached_miss_no_pool_returns_unavailable(async_engine):
    await _purge_bench(async_engine, "_T_MISS_")
    df, err = await load_benchmark_cached(async_engine, None, "_T_MISS_", date(2026, 1, 1))
    assert df.empty
    assert err == "ib_pool_unavailable"


async def test_fetch_disabled_raises_benchmark_unavailable(async_engine, monkeypatch):
    monkeypatch.setenv("XENON_PERF_BENCHMARK_FETCH_ENABLED", "false")
    with pytest.raises(BenchmarkUnavailable):
        await fetch_and_cache_benchmark(async_engine, object(), "SPY", date(2026, 1, 1))
