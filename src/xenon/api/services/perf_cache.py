"""Scope-keyed memoize for performance.compute() (spec Decisions §6).

Market-aware TTL: 60s during the US regular session (xenon.utils.market_calendar:
9:30–16:00 ET on trading days), 30min otherwise. Process-local — multi-worker uvicorn has independent
caches per worker (v1 limitation; v2 follow-up: shared cache).

Correction #26 (perf-rebuild): half-day market sessions are NOT handled
(13:00 ET close → 60s TTL until 16:00). Acceptable for v1; revisit later.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

from xenon.execution.account_scope import AccountScope
from xenon.utils import market_calendar

_TTL_OPEN_SEC = 60
_TTL_CLOSED_SEC = 30 * 60

# {(broker, account_env, broker_account, period_lower): (result, stored_at_epoch)}
_cache: dict[tuple[str, str, str, str], tuple[Any, float]] = {}


def _ttl_for_now(now: dt.datetime | None = None) -> int:
    """Return TTL seconds: 60 during the regular session, else 1800."""
    now = now or market_calendar.now_et()
    return _TTL_OPEN_SEC if market_calendar.is_open(now, strict=False) else _TTL_CLOSED_SEC


def _key(scope: AccountScope, period: str = "YTD") -> tuple[str, str, str, str]:
    return (scope.broker, scope.account_env, scope.broker_account, period.lower())


def clear_cache() -> None:
    """Test helper — wipe the process-local cache."""
    _cache.clear()


async def cached_compute(engine, scope: AccountScope, *, ib_pool=None, period: str = "YTD") -> Any:
    """Memoized wrapper around `xenon.api.services.performance.compute`.

    The cache key includes (broker, account_env, broker_account, period) —
    period switches return live results, not stale YTD.
    """
    # Import lazily to avoid circular dep at module import time.
    from xenon.api.services.performance import compute as _inner

    k = _key(scope, period)
    ttl = _ttl_for_now()
    now = time.time()
    cached = _cache.get(k)
    if cached is not None and (now - cached[1]) < ttl:
        return cached[0]
    result = await _inner(engine, scope, ib_pool=ib_pool, period=period)
    _cache[k] = (result, now)
    return result
