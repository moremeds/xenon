"""Futu (read-only) routes and the lazy FutuClient singleton: /futu/sync, /futu/portfolio.

The singleton state lives here with the code that rebinds it (`global`). Other readers (lifespan shutdown,
health, Futu history loop, /orders/refresh, guards.get_broker_scope) use it as `futu_routes.<name>`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import threading
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
# Helpers: the display snapshot lives in Postgres (no data/*.json)
# ---------------------------------------------------------------------------
#
# A /futu/sync result is stored as an `account_snapshots` row (broker FUTU)
# whose payload has NO `observation` key: a display snapshot. The observation
# loop's rows carry `observation` and serve Gate 4; they read no balances
# (bankroll column 0), so display readers never select them.


def _display_rows():
    from sqlalchemy import desc, select

    from xenon.db.schema import account_snapshots

    return (
        select(account_snapshots.c.payload, account_snapshots.c.snapshot_at)
        .where(
            account_snapshots.c.broker == "FUTU",
            ~account_snapshots.c.payload.has_key("observation"),
        )
        .order_by(desc(account_snapshots.c.snapshot_at), desc(account_snapshots.c.id))
    )


def latest_display_snapshot() -> Optional[dict]:
    """Newest Futu display snapshot payload (any account, like the old single
    cache file), or None when nothing was ever synced."""
    from xenon.db.engine import get_sync_engine

    with get_sync_engine().connect() as conn:
        row = conn.execute(_display_rows().limit(1)).first()
    return dict(row.payload) if row is not None and row.payload else None


def _persist_display_snapshot(client: FutuClient, result: dict) -> bool:
    """Store one /futu/sync result as a FUTU display snapshot for the client's
    matched account. No-op (False) under XENON_READ_ONLY=1 or when the matched
    account is unknown."""
    from decimal import Decimal

    from sqlalchemy import func, insert

    from xenon.api.guards import is_read_only
    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import account_snapshots
    from xenon.execution.account_scope import env_from_trd_env

    if is_read_only():
        return False
    acc_id = getattr(client, "_acc_id", None)
    trd_env = client.trd_env_of_matched_account()
    if acc_id is None or trd_env is None:
        logger.warning("Futu display snapshot not stored: matched account unknown")
        return False
    account_env = env_from_trd_env(trd_env)
    net_liq = (result.get("account_summary") or {}).get("net_liquidation")
    net_liq_d = Decimal(str(net_liq)) if isinstance(net_liq, (int, float)) else None
    with get_sync_engine().begin() as conn:
        conn.execute(
            insert(account_snapshots).values(
                account=str(acc_id),
                bankroll=net_liq_d if net_liq_d is not None else Decimal(0),
                net_liquidation=net_liq_d,
                payload=result,
                broker="FUTU",
                account_env=account_env,
                broker_account=str(acc_id),
                snapshot_at=func.clock_timestamp(),
            )
        )
    return True


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

    Returns the previous display snapshot (Postgres) when the new result has
    warnings AND fewer positions than it. The rejected result is not stored;
    its counts and warnings are logged.

    Returns None when there is nothing to preserve (caller proceeds with
    the normal save path).
    """
    new_warnings = new_result.get("warnings") or []
    new_count = new_result.get("count", 0)
    if not new_warnings:
        return None

    prev = latest_display_snapshot()
    if prev is None:
        return None

    prev_count = prev.get("count", 0)
    if new_count >= prev_count:
        return None  # new snapshot isn't degraded

    logger.warning(
        "Futu sync degraded (new=%d, prev=%d, warnings=%s) — preserved prior snapshot",
        new_count,
        prev_count,
        new_warnings[:20],
    )
    return prev


@router.post("/futu/sync")
async def futu_sync():
    """Force-fetch Futu positions + account, store a FUTU display snapshot in Postgres.

    Patterns:
    - **Singleflight.** Concurrent callers serialize on an asyncio lock AND
      share the cached result when inside the cooldown window, so two
      browser tabs hitting Refresh simultaneously produce exactly one
      OpenD roundtrip.
    - **10s cooldown.** Repeat calls within 10s return the cached result.
    - **Partial-failure preservation (tribunal T15).** If the new snapshot
      has warnings and fewer positions than the previous display snapshot
      (Postgres), that snapshot is returned and the new one is not stored.
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
        cached = latest_display_snapshot()
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
            cached = latest_display_snapshot()
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

        try:
            _persist_display_snapshot(client, result)
        except Exception as exc:  # noqa: BLE001 — the fetch itself succeeded
            logger.warning("Futu display snapshot not stored: %s", exc)
            result = {**result, "warnings": list(result.get("warnings") or []) + ["display_snapshot_not_stored"]}
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


# Canonical "never synced" envelope — used by /futu/portfolio when no display
# snapshot exists yet. HTTP 200 with a failure-shaped body (tribunal T14) so
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
    """Read the newest Futu display snapshot from Postgres.

    Does not hit OpenD. Returns HTTP 200 with `{ok:false, code:"never_synced"}`
    when nothing was synced yet so the UI can render a "click sync" prompt
    distinctly from errors. Clients that need fresh data call `POST /futu/sync`.
    """
    cached = latest_display_snapshot()
    if cached is None:
        return dict(_FUTU_NEVER_SYNCED)
    return {"ok": True, **cached}
