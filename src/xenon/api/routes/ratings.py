"""Analyst ratings: raw Unusual Whales rows for one ticker.

``GET /ticker/ratings?ticker=XYZ`` replaces the removed ``xenon-fetch-analyst``
CLI that the Next.js route used to spawn. It returns the UW
``/api/screener/analysts`` rows unchanged (no derived scores — xenon has no
signal layer), plus ``as_of`` (fetch time) and ``source``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from xenon.clients.uw_client import UWClient

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ratings"])

CACHE_TTL_SECONDS = 15 * 60
# xenon.utils.uw_cache has one fixed global 60s TTL and is already applied
# inside UWClient._get; this longer per-ticker cache sits in front of it.
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()

_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")
_ROW_LIMIT = 50
_UW_TIMEOUT_S = 10
_UW_MAX_RETRIES = 1


def _now() -> float:
    return time.monotonic()


def _error(status: int, reason_code: str, message: str) -> JSONResponse:
    # Top-level reason_code: the web toast helper reads body.reason_code.
    return JSONResponse(status_code=status, content={"reason_code": reason_code, "detail": message})


def _fetch_rows(ticker: str) -> list[dict]:
    """Blocking UW call — run via asyncio.to_thread."""
    client = UWClient(timeout=_UW_TIMEOUT_S, max_retries=_UW_MAX_RETRIES)
    try:
        payload = client.get_analyst_ratings_by_ticker(ticker, limit=_ROW_LIMIT)
    finally:
        client.close()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("unexpected UW analysts payload shape")
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

    try:
        rows = await asyncio.to_thread(_fetch_rows, symbol)
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
