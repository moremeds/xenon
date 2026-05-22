"""Small cache for portfolio flow-analysis summaries.

This is intentionally scoped to `/flow-analysis`. It reuses the existing
Postgres snapshot table as a historical store, but it does not expose or
maintain the removed `/uw-analyze` product surface.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Optional

logger = logging.getLogger("xenon.flow_analysis_cache")

_TTL_OPEN_S = 1800
_TTL_CLOSED_S = 3600
_MAX_PARALLEL_RUNS = 3
_RUN_TIMEOUT_S = 60.0

Source = str
RunnerResult = dict[str, Any]


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _is_market_open_default() -> bool:
    try:
        from xenon.utils.market_hours import is_market_open

        return bool(is_market_open())
    except Exception:  # noqa: BLE001
        return False


class FlowAnalysisCache:
    """In-memory cache for dark-pool/options-flow summaries by ticker."""

    def __init__(
        self,
        *,
        market_open_fn: Callable[[], bool] = _is_market_open_default,
        ttl_open_s: int = _TTL_OPEN_S,
        ttl_closed_s: int = _TTL_CLOSED_S,
        max_parallel: int = _MAX_PARALLEL_RUNS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._market_open_fn = market_open_fn
        self._ttl_open = ttl_open_s
        self._ttl_closed = ttl_closed_s
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._clock = clock
        self._entries: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._loaded = False

    def _ttl(self) -> int:
        return self._ttl_open if self._market_open_fn() else self._ttl_closed

    def _is_fresh(self, entry: dict) -> bool:
        ts_iso = entry.get("ts")
        if not isinstance(ts_iso, str):
            return False
        try:
            ts = datetime.fromisoformat(ts_iso)
        except ValueError:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (_now_utc() - ts).total_seconds() < self._ttl()

    def _lock_for(self, ticker: str) -> asyncio.Lock:
        lock = self._locks.get(ticker)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[ticker] = lock
        return lock

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        url = os.environ.get("DATABASE_URL")
        if not url:
            self._loaded = True
            return
        try:
            from sqlalchemy import create_engine, text

            sync_url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
            engine = create_engine(sync_url)
            with engine.connect() as conn:
                rows = conn.execute(
                    text(
                        "SELECT DISTINCT ON (ticker) "
                        "ticker, dark_pool_summary, options_flow_summary, sources, archived_at "
                        "FROM xenon.uw_analyze_snapshots "
                        "WHERE dark_pool_summary IS NOT NULL OR options_flow_summary IS NOT NULL "
                        "ORDER BY ticker, archived_at DESC"
                    )
                ).fetchall()
            engine.dispose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("flow_analysis_cache PG load failed: %s", exc)
            return

        for row in rows:
            m = row._mapping
            archived_at = m["archived_at"]
            self._entries[m["ticker"]] = {
                "ticker": m["ticker"],
                "dark_pool_summary": m["dark_pool_summary"],
                "options_flow_summary": m["options_flow_summary"],
                "sources": list(m["sources"] or []),
                "ts": archived_at.isoformat() if archived_at else None,
            }
        self._loaded = True
        logger.info("flow_analysis_cache loaded %d entries from Postgres", len(self._entries))

    def get_entry(self, ticker: str) -> Optional[dict]:
        self._ensure_loaded()
        return self._entries.get(ticker.upper())

    async def get_or_run(
        self,
        ticker: str,
        *,
        runner: Callable[[str], Awaitable[RunnerResult]],
        force: bool = False,
        sources: Optional[Iterable[Source]] = None,
    ) -> tuple[dict, bool]:
        ticker = ticker.upper()
        self._ensure_loaded()
        async with self._lock_for(ticker):
            entry = self._entries.get(ticker)
            if entry and not force and self._is_fresh(entry):
                self._merge_sources(entry, sources or ())
                return entry, False

            async with self._semaphore:
                result = await asyncio.wait_for(runner(ticker), timeout=_RUN_TIMEOUT_S)

            entry = {
                "ticker": ticker,
                "dark_pool_summary": result.get("dark_pool_summary"),
                "options_flow_summary": result.get("options_flow_summary"),
                "sources": sorted(set(sources or ())),
                "ts": _now_iso(),
            }
            self._entries[ticker] = entry
            await asyncio.to_thread(self._archive_to_postgres, entry)
            return entry, True

    def _merge_sources(self, entry: dict, sources: Iterable[Source]) -> None:
        if not sources:
            return
        entry["sources"] = sorted(set(entry.get("sources") or []) | set(sources))

    @staticmethod
    def _archive_to_postgres(entry: dict) -> None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            return
        try:
            from sqlalchemy import create_engine, insert

            from xenon.db.schema import uw_analyze_snapshots

            sync_url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
            engine = create_engine(sync_url)
            with engine.begin() as conn:
                conn.execute(
                    insert(uw_analyze_snapshots).values(
                        ticker=entry["ticker"],
                        dark_pool_summary=entry.get("dark_pool_summary"),
                        options_flow_summary=entry.get("options_flow_summary"),
                        sources=entry.get("sources"),
                        archived_at=_now_utc(),
                    )
                )
            engine.dispose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("flow_analysis_cache archive failed for %s: %s", entry.get("ticker"), exc)
