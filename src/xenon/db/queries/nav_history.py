"""Query helpers for xenon.nav_history + xenon.benchmark_closes.

Spec: docs/superpowers/specs/2026-05-31-performance-rebuild-design.md
      § Architecture > Components.

Corrections applied:
  - #9: IBPool surface — uses `ib_pool.get("data")` (single method) not
        `with_role`/`contract_for` (don't exist). IB fetch defers via a
        runtime guard so v1 can ship without the full IB integration.
  - #10: IB historical bar dates normalized via pd.to_datetime(...).date().
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Tuple

import pandas as pd
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from xenon.db.schema import benchmark_closes, nav_history
from xenon.execution.account_scope import AccountScope

logger = logging.getLogger(__name__)


class BenchmarkUnavailable(Exception):
    """Raised when the IB pool cannot serve a benchmark fetch."""


async def load_nav_curve(
    engine: AsyncEngine,
    scope: AccountScope,
    period_start: date,
    period_end: date | None = None,
) -> pd.DataFrame:
    """Return DataFrame[date, nav, daily_pnl, source] ascending by date,
    scope-filtered, optionally upper-bounded by ``period_end`` (inclusive).

    Prefer-close semantics (Pass-1 addition): when two rows exist for the
    same date with different ``source`` values, the ``close`` row wins. Under
    the post-2026_06_03 schema the PK includes ``source`` so intraday + close
    rows coexist; the ``DISTINCT ON (date)`` keeps the chart from
    double-counting. Pre-migration scopes (one row per date) get a no-op.
    """
    where = (
        (nav_history.c.broker == scope.broker)
        & (nav_history.c.account_env == scope.account_env)
        & (nav_history.c.broker_account == scope.broker_account)
        & (nav_history.c.date >= period_start)
    )
    if period_end is not None:
        where = where & (nav_history.c.date <= period_end)

    # DISTINCT ON (date) — PG-specific. ORDER BY date ASC for the outer
    # iteration, then source ranked so 'close' (rank=0) wins over 'intraday'
    # (rank=1) when both rows exist for the same date.
    source_rank = sa.case(
        (nav_history.c.source == "close", 0),
        else_=1,
    ).label("_source_rank")

    stmt = (
        sa.select(
            nav_history.c.date,
            nav_history.c.nav,
            nav_history.c.daily_pnl,
            nav_history.c.source,
            source_rank,
        )
        .where(where)
        .distinct(nav_history.c.date)
        .order_by(nav_history.c.date.asc(), source_rank.asc())
    )

    async with engine.begin() as conn:
        result = await conn.execute(stmt)
        rows = result.fetchall()
    df = pd.DataFrame(rows, columns=["date", "nav", "daily_pnl", "source", "_source_rank"])
    df = df.drop(columns=["_source_rank"])
    if not df.empty:
        df["nav"] = df["nav"].astype(float)
        df["daily_pnl"] = df["daily_pnl"].astype(float).where(df["daily_pnl"].notna(), None)
    return df


async def load_inception_date(engine: AsyncEngine, scope: AccountScope) -> date | None:
    """Return the earliest ``nav_history.date`` for the scope, or None.

    Used by the /performance route to resolve ``period=All`` to a concrete
    start date. Cheap (PK-prefix indexed) so safe to call on every request.
    """
    async with engine.begin() as conn:
        result = await conn.execute(
            sa.select(sa.func.min(nav_history.c.date)).where(
                (nav_history.c.broker == scope.broker)
                & (nav_history.c.account_env == scope.account_env)
                & (nav_history.c.broker_account == scope.broker_account)
            )
        )
        row = result.first()
    if row is None or row[0] is None:
        return None
    return row[0]


async def load_benchmark_cached(
    engine: AsyncEngine, ib_pool, symbol: str, period_start: date
) -> Tuple[pd.DataFrame, str | None]:
    """Return (DataFrame[date, close], error_reason_or_None).

    Cache-only path returns whatever rows exist in xenon.benchmark_closes for
    (symbol, date >= period_start). If the cache is empty AND ib_pool is
    available, fetch via fetch_and_cache_benchmark; catch failures and surface
    them as the error_reason so the service can render a partial chart with
    a 'benchmark_unavailable' warning.
    """
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                sa.select(benchmark_closes.c.date, benchmark_closes.c.close)
                .where((benchmark_closes.c.symbol == symbol) & (benchmark_closes.c.date >= period_start))
                .order_by(benchmark_closes.c.date.asc())
            )
        ).fetchall()
    df = pd.DataFrame(rows, columns=["date", "close"])
    if not df.empty:
        df["close"] = df["close"].astype(float)
        return df, None

    # Cache miss. Attempt IB fetch only if we have a pool.
    if ib_pool is None:
        return df, "ib_pool_unavailable"
    try:
        await fetch_and_cache_benchmark(engine, ib_pool, symbol, period_start)
    except Exception as exc:
        logger.warning("benchmark fetch failed: %s", exc)
        return df, str(exc)

    # Re-query post-fetch.
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                sa.select(benchmark_closes.c.date, benchmark_closes.c.close)
                .where((benchmark_closes.c.symbol == symbol) & (benchmark_closes.c.date >= period_start))
                .order_by(benchmark_closes.c.date.asc())
            )
        ).fetchall()
    df = pd.DataFrame(rows, columns=["date", "close"])
    if not df.empty:
        df["close"] = df["close"].astype(float)
    return df, None


async def fetch_and_cache_benchmark(engine: AsyncEngine, ib_pool, symbol: str, period_start: date) -> None:
    """Fetch daily closes from IB pool's 'data' role and upsert into benchmark_closes.

    Uses IBPool.get("data") (correction #9). Normalizes bar dates via
    pd.to_datetime to avoid TypeError on string formats (correction #10).
    Raises BenchmarkUnavailable when no data role is available.

    v1 NOTE: deferred — this is gated behind XENON_PERF_BENCHMARK_FETCH_ENABLED
    (default false). When false, raises BenchmarkUnavailable immediately so the
    cache-only path is the only live path. When true, the IB integration runs.
    """
    import asyncio
    import os

    if os.environ.get("XENON_PERF_BENCHMARK_FETCH_ENABLED", "false").lower() != "true":
        raise BenchmarkUnavailable("benchmark fetch disabled (set XENON_PERF_BENCHMARK_FETCH_ENABLED=true to enable)")

    client = ib_pool.get("data") if hasattr(ib_pool, "get") else None
    if client is None:
        raise BenchmarkUnavailable("no IB data role available")

    def _sync_fetch():
        from ib_async import Stock

        contract = Stock(symbol, "SMART", "USD")
        ib = client.ib
        bars = ib.reqHistoricalData(
            contract=contract,
            endDateTime="",
            durationStr="2 Y",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        return [{"date": pd.to_datetime(b.date).date(), "close": float(b.close)} for b in bars]

    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, _sync_fetch)
    if not rows:
        return

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    async with engine.begin() as conn:
        for r in rows:
            if r["date"] < period_start:
                continue
            stmt = (
                pg_insert(benchmark_closes)
                .values(symbol=symbol, date=r["date"], close=r["close"])
                .on_conflict_do_update(
                    index_elements=["symbol", "date"],
                    set_={"close": r["close"]},
                )
            )
            await conn.execute(stmt)
