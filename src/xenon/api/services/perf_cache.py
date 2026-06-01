"""Scope-keyed memoize for performance.compute() (spec Decisions §6).

Market-aware TTL: 60s during US market hours (9:30–16:00 ET Mon-Fri),
30min otherwise. Process-local — multi-worker uvicorn has independent
caches per worker (v1 limitation; v2 follow-up: shared cache).

Correction #26 (perf-rebuild): half-day market sessions are NOT handled
(13:00 ET close → 60s TTL until 16:00). Acceptable for v1; revisit later.
"""
from __future__ import annotations

import datetime as dt
import time
import zoneinfo
from typing import Any

from xenon.execution.account_scope import AccountScope

_ET = zoneinfo.ZoneInfo("America/New_York")
_TTL_OPEN_SEC = 60
_TTL_CLOSED_SEC = 30 * 60

# {(broker, account_env, broker_account): (result, stored_at_epoch)}
_cache: dict[tuple[str, str, str], tuple[Any, float]] = {}


def _ttl_for_now(now: dt.datetime | None = None) -> int:
    """Return TTL seconds: 60 if open RTH, else 1800."""
    now = now or dt.datetime.now(tz=_ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_ET)
    else:
        now = now.astimezone(_ET)
    if now.weekday() >= 5:  # Sat / Sun
        return _TTL_CLOSED_SEC
    minutes = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return _TTL_OPEN_SEC
    return _TTL_CLOSED_SEC


def _key(scope: AccountScope) -> tuple[str, str, str]:
    return (scope.broker, scope.account_env, scope.broker_account)


def clear_cache() -> None:
    """Test helper — wipe the process-local cache."""
    _cache.clear()


async def cached_compute(engine, scope: AccountScope, *, ib_pool=None) -> Any:
    """Memoized wrapper around `xenon.api.services.performance.compute`."""
    # Import lazily to avoid circular dep at module import time.
    from xenon.api.services.performance import compute as _inner

    k = _key(scope)
    ttl = _ttl_for_now()
    now = time.time()
    cached = _cache.get(k)
    if cached is not None and (now - cached[1]) < ttl:
        return cached[0]
    result = await _inner(engine, scope, ib_pool=ib_pool)
    _cache[k] = (result, now)
    return result


def warm(engine, scope: AccountScope, *, ib_pool=None) -> None:
    """Fire-and-forget warmup. Used by deprecated POST /performance/background.

    Returns immediately; the compute happens on the event loop in the background.
    """
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — caller must arrange one (test scaffolding).
        return
    loop.create_task(cached_compute(engine, scope, ib_pool=ib_pool))
