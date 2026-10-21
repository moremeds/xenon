"""Analyst ratings: raw Unusual Whales rows for one ticker.

``GET /ticker/ratings?ticker=XYZ`` replaces the removed ``xenon-fetch-analyst``
CLI that the Next.js route used to spawn. It returns the UW
``/api/screener/analysts`` rows unchanged (no derived scores — xenon has no
signal layer), plus ``as_of`` (fetch time) and ``source``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from xenon.clients.uw_client import UWClient
from xenon.utils import uw_cache

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ratings"])

CACHE_TTL_SECONDS = 15 * 60
# xenon.utils.uw_cache has one fixed global 60s TTL and is already applied
# inside UWClient._get; this longer per-ticker cache sits in front of it.
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()

_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")
_ROW_LIMIT = 50
_UW_ENDPOINT = "screener/analysts"  # UWClient.get_analyst_ratings; keeps the uw_cache key in sync (tested)

# Deadline and resource limits (web proxy `xenonFetch` aborts at 20 s):
#   - one UW attempt, no retries, so no Retry-After / backoff sleeps;
#   - UW_TIMEOUT_S is the requests connect/read timeout. It measures socket
#     inactivity, not total duration, and does not cover DNS, so it does NOT
#     bound how long a worker thread runs;
#   - the route stops waiting after REQUEST_DEADLINE_S and answers 504;
#   - the blocking fetch runs on a private pool of MAX_IN_FLIGHT threads. A
#     worker stuck past the deadline keeps its slot until it returns, so stuck
#     UW reads can never use more than MAX_IN_FLIGHT threads or touch the
#     default executor. When every slot is busy the route answers 503 at once.
UW_TIMEOUT_S = 8
UW_MAX_RETRIES = 0
REQUEST_DEADLINE_S = 12
MAX_IN_FLIGHT = 2

_executor = ThreadPoolExecutor(max_workers=MAX_IN_FLIGHT, thread_name_prefix="uw_ratings")
_in_flight = 0
_in_flight_lock = threading.Lock()

_TEXT_FIELDS = ("timestamp", "firm", "analyst_name", "action", "recommendation")


def _now() -> float:
    return time.monotonic()


def _error(status: int, reason_code: str, message: str) -> JSONResponse:
    # Top-level reason_code: the web toast helper reads body.reason_code.
    return JSONResponse(status_code=status, content={"reason_code": reason_code, "detail": message})


def _validate_rows(rows: list) -> None:
    """Reject rows the web layer cannot map; raises ValueError."""
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("analyst row is not an object")
        for key in _TEXT_FIELDS:
            if row.get(key) is not None and not isinstance(row[key], str):
                raise ValueError(f"analyst row field {key} is not a string")
        target = row.get("target")
        if target is None or target == "":
            continue
        if isinstance(target, bool) or not isinstance(target, (int, float, str)):
            raise ValueError("analyst row target is not numeric")
        try:
            ok = math.isfinite(float(target))
        except (ValueError, OverflowError, TypeError):  # e.g. a 400-digit int
            ok = False
        if not ok:
            raise ValueError("analyst row target is not numeric")


def _acquire_slot() -> bool:
    global _in_flight
    with _in_flight_lock:
        if _in_flight >= MAX_IN_FLIGHT:
            return False
        _in_flight += 1
        return True


def _release_slot() -> None:
    global _in_flight
    with _in_flight_lock:
        _in_flight -= 1


def _fetch_rows(ticker: str) -> list[dict]:
    """Blocking UW call — runs on the private `_executor`. The admission slot
    is owned by the caller (released by a done-callback on the executor future)."""
    client = UWClient(timeout=UW_TIMEOUT_S, max_retries=UW_MAX_RETRIES)
    try:
        payload = client.get_analyst_ratings_by_ticker(ticker, limit=_ROW_LIMIT)
    finally:
        client.close()
    try:
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("unexpected UW analysts payload shape")
        _validate_rows(rows)
    except ValueError:
        # UWClient._get already put this HTTP-200 body in the shared 60 s uw_cache;
        # a rejected payload must not be served again once upstream recovers.
        uw_cache.evict(uw_cache.make_key(_UW_ENDPOINT, {"ticker": ticker, "limit": _ROW_LIMIT}))
        raise
    return rows


@router.get("/ticker/ratings")
async def ticker_ratings(ticker: str | None = None):
    symbol = (ticker or "").strip().upper()
    if not _TICKER_RE.fullmatch(symbol):
        return _error(400, "INVALID_TICKER", "ticker must match [A-Z0-9.-]{1,10}")

    with _cache_lock:
        hit = _cache.get(symbol)
    if hit is not None and _now() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]

    if not _acquire_slot():
        logger.warning("UW analyst ratings: all %d workers busy, refusing %s", MAX_IN_FLIGHT, symbol)
        return _error(503, "RATINGS_BUSY", "Analyst ratings fetch is busy; retry shortly")
    # The slot is released exactly once when the executor future finishes OR is
    # cancelled (cancelled while still queued, the job never runs). A job that is
    # already running keeps its slot until the blocking fetch returns.
    try:
        cfut = _executor.submit(_fetch_rows, symbol)
    except BaseException:
        _release_slot()
        raise
    cfut.add_done_callback(lambda _f: _release_slot())
    try:
        rows = await asyncio.wait_for(asyncio.wrap_future(cfut), timeout=REQUEST_DEADLINE_S)
    except asyncio.TimeoutError:
        logger.warning("UW analyst ratings fetch for %s exceeded %ss", symbol, REQUEST_DEADLINE_S)
        return _error(504, "UW_TIMEOUT", "Unusual Whales analyst ratings timed out")
    except Exception as exc:  # UWAPIError, network, bad payload shape
        logger.warning("UW analyst ratings fetch failed for %s: %s", symbol, type(exc).__name__)
        return _error(502, "UW_UPSTREAM_ERROR", "Unusual Whales analyst ratings unavailable")

    body = {
        "ticker": symbol,
        "source": "unusual_whales",
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ratings": rows,
    }
    with _cache_lock:
        _cache[symbol] = (_now(), body)
    return body
