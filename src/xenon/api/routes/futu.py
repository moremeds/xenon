"""Futu (read-only) routes and the lazy FutuClient singleton: /futu/sync, /futu/portfolio.

The singleton state lives here with the code that rebinds it (`global`). Other readers (lifespan shutdown,
health, Futu history loop, /orders/refresh, guards.get_broker_scope) use it as `futu_routes.<name>`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

from xenon.clients.futu_client import FutuClient
from xenon.clients.futu_exceptions import FutuConnectionError, FutuError

router = APIRouter()
logger = logging.getLogger("xenon.api")


# Futu singleton — lazy-initialized on first /futu/sync call so the server
# boots cleanly even when OpenD is not running. Guarded by an asyncio lock
# (singleflight) so concurrent HTTP requests collapse to one OpenD roundtrip.
_futu_client: Optional[FutuClient] = None
_futu_lock: Optional[asyncio.Lock] = None
# `None` sentinel (not 0.0) so the first-call cooldown check does not fire.
# time.monotonic() returns small values near process start, and `now - 0.0`
# would look "recently synced" and serve stale cache instead of a real fetch.
_futu_last_sync_monotonic: Optional[float] = None
FUTU_COOLDOWN_S = 10
# Hard bound on OpenD calls — matches the 15s bound in _futu_refresh_db_first.
# A port-open but handshaking-hung OpenD must fail the route, not wedge it.
FUTU_SYNC_TIMEOUT_S = 15.0
# Single-worker executor for /futu/sync: the blocking SDK call runs here so at
# most one OpenD call is ever in flight. `_futu_sync_future` tracks the worker
# at thread level — asyncio.wait_for cancels the await on timeout but cannot
# kill the thread, so the future stays the in-flight marker until the worker
# actually exits.
_futu_sync_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="xenon-futu-sync"
)
_futu_sync_future: Optional[concurrent.futures.Future] = None


def _get_futu_lock() -> asyncio.Lock:
    global _futu_lock
    if _futu_lock is None:
        _futu_lock = asyncio.Lock()
    return _futu_lock


def _get_futu_client() -> FutuClient:
    """Lazy-init the shared FutuClient singleton."""
    global _futu_client
    if _futu_client is None:
        _futu_client = FutuClient(
            host=os.environ.get("FUTU_OPEND_HOST", "127.0.0.1"),
            port=int(os.environ.get("FUTU_OPEND_PORT", "11111")),
            security_firm=os.environ.get("FUTU_SECURITY_FIRM", "FUTUSECURITIES"),
            trd_env=os.environ.get("FUTU_TRD_ENV", "REAL"),
            filter_trading_market=os.environ.get("FUTU_MARKET", "US"),
        )
    return _futu_client


def _futu_fetch_portfolio_blocking(client: FutuClient) -> dict:
    """Blocking /futu/sync work: connect if needed, then force-fetch.

    Runs on the dedicated single-worker executor so the whole call is one
    thread — a hang holds the client's RLock but never multiplies workers.
    """
    if not client.is_connected():
        client.connect()
    return client.fetch_portfolio(force=True)


def _futu_cleanup_after_timeout(
    client: FutuClient,
    fetch: "concurrent.futures.Future",
    marker: "concurrent.futures.Future",
) -> None:
    """Daemon-thread cleanup after a /futu/sync timeout.

    The Futu SDK's ``close()`` takes its own RLock — running ``disconnect()``
    on the event loop would block every request while the SDK is stuck.
    Completes the combined in-flight marker only after BOTH the hung fetch
    worker and the reset have finished, so a late cleanup can never close a
    newer session.
    """
    try:
        client.disconnect()
    except Exception:
        logger.exception("Futu client reset after sync timeout failed")
    try:
        fetch.result()
    except Exception:
        pass
    marker.set_result(None)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_cache(path: Path) -> Optional[dict]:
    """Read a JSON cache file, return None if missing/corrupt."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _write_cache(path: Path, data: dict) -> None:
    """Write JSON to cache file atomically via temp file + os.replace()."""
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp", prefix=".cache_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _atomic_save(path: str, data: dict) -> str:
    """Use the project's atomic_save for portfolio/orders files."""
    from xenon.utils.atomic_io import atomic_save

    return atomic_save(path, data)


# ---------------------------------------------------------------------------
# Phase 4: Market data & long-running endpoints (subprocess-based)
# ---------------------------------------------------------------------------


def _futu_in_cooldown() -> bool:
    """True if the last successful sync was less than FUTU_COOLDOWN_S ago.

    Uses a None sentinel for "never synced this process" so the first call
    after startup always proceeds to a real fetch.
    """
    if _futu_last_sync_monotonic is None:
        return False
    import time as _time

    return _time.monotonic() - _futu_last_sync_monotonic < FUTU_COOLDOWN_S


def _maybe_preserve_partial_failure(new_result: dict) -> Optional[dict]:
    """Refuse to overwrite a good snapshot with a degraded one.

    Returns the previous cached file when the new result has warnings AND
    fewer positions than the cache. Writes a sidecar
    `data/futu_portfolio.error.json` so the rejected payload is inspectable.

    Returns None when there is nothing to preserve (caller proceeds with
    the normal save path).
    """
    from xenon.api import server

    new_warnings = new_result.get("warnings") or []
    new_count = new_result.get("count", 0)
    if not new_warnings:
        return None

    cached_path = server.DATA_DIR / "futu_portfolio.json"
    prev = _read_cache(cached_path)
    if prev is None:
        return None

    prev_count = prev.get("count", 0)
    if new_count >= prev_count:
        return None  # new snapshot isn't degraded

    error_path = server.DATA_DIR / "futu_portfolio.error.json"
    try:
        _write_cache(error_path, new_result)
    except Exception as exc:
        logger.warning("Failed to write futu error sidecar: %s", exc)
    logger.warning(
        "Futu sync degraded (new=%d, prev=%d, warnings=%d) — preserved prior snapshot",
        new_count,
        prev_count,
        len(new_warnings),
    )
    return prev


@router.post("/futu/sync")
async def futu_sync():
    """Force-fetch Futu positions + account, write data/futu_portfolio.json.

    Patterns:
    - **Singleflight.** Concurrent callers serialize on an asyncio lock AND
      share the cached result when inside the cooldown window, so two
      browser tabs hitting Refresh simultaneously produce exactly one
      OpenD roundtrip.
    - **10s cooldown.** Repeat calls within 10s return the cached result.
    - **Partial-failure preservation (tribunal T15).** If the new snapshot
      has warnings and fewer positions than the cache, the cache is
      preserved untouched and a sidecar error file is written.
    - **Off-loop blocking calls.** The Futu SDK is fully sync; the call runs
      on a dedicated single-worker executor. While a worker is still alive
      (e.g. after a timeout), new requests 503 instead of queueing behind the
      client's RLock; a timeout also resets the client via ``disconnect()``.
    """
    from xenon.api import server

    global _futu_last_sync_monotonic, _futu_sync_future
    import time as _time

    lock = _get_futu_lock()

    # Early cooldown check — avoids lock contention when cache is fresh.
    if _futu_in_cooldown():
        cached = _read_cache(server.DATA_DIR / "futu_portfolio.json")
        if cached:
            return {"ok": True, **cached}

    # A timed-out SDK call keeps its worker thread — and the client's RLock —
    # alive. Never queue a second worker behind it.
    inflight = _futu_sync_future
    if inflight is not None and not inflight.done():
        raise HTTPException(
            status_code=503, detail="Futu sync already in flight — retry shortly"
        )

    async with lock:
        # Re-check inside lock — race guard so two tabs inside the lock
        # window piggyback on the same result.
        if _futu_in_cooldown():
            cached = _read_cache(server.DATA_DIR / "futu_portfolio.json")
            if cached:
                return {"ok": True, **cached}

        inflight = _futu_sync_future
        if inflight is not None and not inflight.done():
            raise HTTPException(
                status_code=503, detail="Futu sync already in flight — retry shortly"
            )

        client = _get_futu_client()
        fetch = _futu_sync_executor.submit(_futu_fetch_portfolio_blocking, client)
        _futu_sync_future = fetch
        try:
            result = await asyncio.wait_for(
                asyncio.wrap_future(fetch),
                timeout=FUTU_SYNC_TIMEOUT_S,
            )
        except TimeoutError:
            # The fetch worker is still running and may hold the client's
            # RLock. Cleanup must NOT run on the event loop — the SDK's
            # close() takes its own RLock and would block every request.
            # Swap the marker for a combined future that completes only when
            # BOTH the hung fetch and the daemon cleanup thread finish, so a
            # late cleanup can never close a newer session.
            marker = concurrent.futures.Future()
            _futu_sync_future = marker
            try:
                threading.Thread(
                    target=_futu_cleanup_after_timeout,
                    args=(client, fetch, marker),
                    name="xenon-futu-cleanup",
                    daemon=True,
                ).start()
            except Exception:
                # Thread spawn failed (e.g. RuntimeError: cannot start new
                # thread). Never fall back to disconnect() on the loop — the
                # marker completes when the fetch worker exits instead, so the
                # endpoint is not stuck at 503 forever.
                logger.exception("Futu cleanup thread failed to start; marker completes with fetch")
                fetch.add_done_callback(
                    lambda _f: marker.set_result(None) if not marker.done() else None
                )
            raise HTTPException(
                status_code=503,
                detail=f"Futu OpenD did not respond within {FUTU_SYNC_TIMEOUT_S:.0f}s",
            )
        except FutuConnectionError as exc:
            raise HTTPException(status_code=503, detail=f"Futu OpenD unreachable: {exc}")
        except FutuError as exc:
            raise HTTPException(status_code=502, detail=f"Futu error: {exc}")

        # Partial-failure guard: if this snapshot looks worse than the
        # cache, keep the cache and return it with a warning tag.
        preserved = _maybe_preserve_partial_failure(result)
        if preserved is not None:
            preserved_warnings = list(preserved.get("warnings") or []) + [
                "partial_failure_preserved",
            ]
            return {"ok": True, **preserved, "warnings": preserved_warnings}

        _atomic_save(str(server.DATA_DIR / "futu_portfolio.json"), result)
        _futu_last_sync_monotonic = _time.monotonic()

        # Persist FUTU NAV to xenon.nav_history (spec §10). Best-effort —
        # a persistence failure must NOT mask a successful OpenD fetch.
        # NavAccountEnvConflict bubbles to a 409 so the operator sees it.
        engine = getattr(server.app.state, "db_engine", None)
        if engine is not None:
            from xenon.api.services.futu_nav_persistence import (
                NavAccountEnvConflict,
                persist_futu_nav,
            )

            matched_env = client.trd_env_of_matched_account()
            try:
                await persist_futu_nav(engine, client, matched_env, result)
            except NavAccountEnvConflict as exc:
                raise HTTPException(status_code=409, detail=str(exc))
            except Exception as exc:  # noqa: BLE001
                logger.warning("persist_futu_nav failed (continuing): %s", exc)

        return {"ok": True, **result}


# Canonical "never synced" envelope — used by /futu/portfolio when no cache
# file exists yet. HTTP 200 with a failure-shaped body (tribunal T14) so
# the UI can distinguish "first boot" from "sync failed" without a 404
# flashing an error state.
_FUTU_NEVER_SYNCED = {
    "ok": False,
    "code": "never_synced",
    "positions": [],
    "count": 0,
    "account_summary": None,
    "fetched_at": None,
    "data_as_of": None,
}


@router.get("/futu/portfolio")
async def futu_portfolio():
    """Read the cached Futu portfolio snapshot.

    Does not hit OpenD — serves the last written `data/futu_portfolio.json`.
    Returns HTTP 200 with `{ok:false, code:"never_synced"}` on first boot
    so the UI can render a "click sync" prompt distinctly from errors.
    Clients that need fresh data call `POST /futu/sync`.
    """
    from xenon.api import server

    cached = _read_cache(server.DATA_DIR / "futu_portfolio.json")
    if cached is None:
        return dict(_FUTU_NEVER_SYNCED)
    return {"ok": True, **cached}
