"""Postgres advisory-lock helper for singleton background loops.

In multi-worker FastAPI deployments (gunicorn --workers N) every worker
runs the same `lifespan` startup. For loops that must be singletons —
the upcoming VCG/CRI scanner loop, the existing UW-daily worker — only
one worker should actually run the work; the rest should detect the
contention and exit cleanly.

Postgres advisory locks are session-scoped. Once acquired via
`pg_try_advisory_lock(key)`, the lock survives until either the
session is closed or `pg_advisory_unlock(key)` is called. This makes
them ideal for "I am the only worker running this loop" guards.

Usage:

    from xenon.api.services.advisory_lock import (
        LOCK_KEY_VCG_CRI,
        pg_try_advisory_lock,
    )

    async def _vcg_cri_supervised() -> None:
        async with pg_try_advisory_lock(LOCK_KEY_VCG_CRI) as got_lock:
            if not got_lock:
                logger.info("vcg_cri loop already running on another worker")
                return
            await _vcg_cri_run_loop()

The lock is held for the lifetime of the `async with` block, so the
loop must run *inside* the block — exiting the block releases the lock.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from xenon.db.engine import get_engine

logger = logging.getLogger(__name__)


# Stable 64-bit keys for xenon background loops. Each loop owns a
# distinct key so concurrent loops never block each other. Add a
# new entry here when introducing a new singleton loop.
LOCK_KEY_UW_DAILY = 7341001
LOCK_KEY_VCG_CRI = 7342001


@asynccontextmanager
async def pg_try_advisory_lock(
    key: int,
    *,
    engine: AsyncEngine | None = None,
) -> AsyncIterator[bool]:
    """Try to acquire a Postgres advisory lock. Yields whether we got it.

    Non-blocking: if the lock is held elsewhere we yield False immediately.
    The held connection is dedicated to the lock for the duration of the
    block, so the caller should only enter for long-running supervisor
    work — not per-request paths.

    The lock is released both explicitly on exit and implicitly when the
    underlying session closes, so a crashed worker doesn't leave a
    permanent ghost lock.
    """
    eng = engine if engine is not None else get_engine()
    conn = await eng.connect()
    got_lock = False
    try:
        result = await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
        got_lock = bool(result.scalar())
        try:
            yield got_lock
        finally:
            if got_lock:
                try:
                    await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                except Exception:
                    # Connection may already be closed by an outer error;
                    # the lock dies with the session anyway.
                    logger.debug("pg_advisory_unlock(%d) failed; relying on session-end release", key)
    finally:
        await conn.close()
