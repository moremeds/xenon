"""Portfolio routes: /attribution, /portfolio, /portfolio/sync, /portfolio/background-sync, /orders/refresh.

Owns the single-flight sync task (`_sync_task`) and the PR-11 naked-short audit enforce hook that runs inside
it (`_sync_via_subprocess` -> `_enforce_naked_short_audit` -> `audit_enforce.enforce`). The IB script runner,
the order owner and app.state are read from server.py at call time.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from xenon.api.guards import (
    ensure_order_allowed,
    get_account_scope,
    get_broker_scope,
    is_read_only,
    require_mode_verified,
)
from xenon.api.routes import futu as futu_routes
from xenon.api.routes.orders import orders_payload_for_scope
from xenon.api.services import audit_enforce, order_entry
from xenon.api.subprocess import ScriptResult, run_entry_point
from xenon.clients.ib_client import DEFAULT_GATEWAY_PORT, pool_client_id

router = APIRouter()
logger = logging.getLogger("xenon.api")


@router.get("/attribution")
async def attribution():
    """Run portfolio attribution (portfolio_attribution.py --json)."""
    result = await run_entry_point("xenon-portfolio-attrib", ["--json"], timeout=15)
    if not result.ok:
        raise HTTPException(status_code=500, detail=result.error)
    return result.data


# ---------------------------------------------------------------------------
# Phase 2: IB file-writer endpoints
# ---------------------------------------------------------------------------


async def _read_portfolio_payload(scope) -> dict:
    """Load the latest structured portfolio payload from Postgres for the scope.

    Phase 1 of the portfolio postgres read-path migration — replaces the prior
    `data/portfolio.json` reader. The payload is stamped at sync time by
    `_save_portfolio_to_postgres` in `xenon.execution.ib_sync`.
    See docs/plans/2026-04-27-portfolio-postgres-read-path.md.
    """
    from xenon.db.engine import get_engine
    from xenon.db.queries.portfolio import get_latest_portfolio_payload

    engine = get_engine()
    async with engine.connect() as conn:
        return await get_latest_portfolio_payload(
            conn,
            broker=scope.broker,
            account_env=scope.account_env,
            broker_account=scope.broker_account,
        )


@router.get("/portfolio")
async def portfolio_get(scope=Depends(get_account_scope)):
    """Return the latest portfolio snapshot from Postgres, scoped to the
    current broker account. Shape matches `web/lib/portfolioDataSchema.ts`.
    Returns 404 when no snapshot exists yet — callers should POST
    /portfolio/sync (or /portfolio/background-sync) to populate.
    """
    payload = await _read_portfolio_payload(scope)
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No portfolio snapshot for scope "
                f"{scope.broker}/{scope.account_env}/{scope.broker_account} — "
                f"run POST /portfolio/sync to populate."
            ),
        )
    return payload


def _snapshot_age_seconds(payload: dict) -> Optional[float]:
    """Age in seconds of a portfolio payload's `last_sync` stamp, or None when
    absent/unparseable. `last_sync` is naive-local ISO from ib_sync (same host
    as the API), so compare against naive `datetime.now()`."""
    raw = payload.get("last_sync") if isinstance(payload, dict) else None
    if not raw:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(raw)).total_seconds()
    except (ValueError, TypeError):
        return None


# A healthy /portfolio/sync rebuilds last_sync every run; a snapshot older than
# this after a "successful" sync means the subprocess exited 0 but the PG write
# never landed (e.g. a schema drift silently swallowed by save_portfolio). Loud,
# not fatal — surfaced in the API log where it's actually visible.
_STALE_SNAPSHOT_AFTER_SYNC_S = 300.0


@router.post("/portfolio/sync")
async def portfolio_sync(scope=Depends(get_account_scope)):
    """Sync portfolio from IB via subprocess, then return the fresh payload.

    Scripts auto-allocate client IDs from subprocess range (20-49).
    Auto-restarts IB Gateway on ECONNREFUSED and retries once. Reads the
    just-written snapshot from Postgres (no longer hits data/portfolio.json).

    Single-flight: if a sync is already running (background or foreground),
    this request waits for it and returns its result instead of starting a
    second pass. `shield` keeps a client disconnect from cancelling the
    shared sync.
    """
    from xenon.api import server

    result = await asyncio.shield(_start_or_join_sync())
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    payload = await _read_portfolio_payload(scope)
    if payload is None:
        raise HTTPException(
            status_code=502,
            detail="ib_sync ran but no snapshot landed in Postgres — check ib_sync logs.",
        )
    # Subprocess exited 0 but the snapshot is stale → the PG write was swallowed
    # (save_portfolio catches write errors). Don't fail the read, but make it loud.
    age = _snapshot_age_seconds(payload)
    if age is not None and age > _STALE_SNAPSHOT_AFTER_SYNC_S and not server._is_test_mode():
        logger.error(
            "portfolio sync returned a stale snapshot (last_sync age %.0fs > %.0fs) for "
            "%s/%s/%s — ib_sync exited 0 but the Postgres write likely failed; check ib_sync logs.",
            age,
            _STALE_SNAPSHOT_AFTER_SYNC_S,
            scope.broker,
            scope.account_env,
            scope.broker_account,
        )
    return payload


# Single-flight slot for every `xenon-ib-sync --sync` spawn in this process
# (`/portfolio/sync` and `/portfolio/background-sync`): at most one sync pass
# (and its post-sync audit) runs at a time. Also keeps a strong reference so
# the task is not garbage-collected mid-run. Syncs started outside the API
# process (reports, CLI, cron) do not share it.
_sync_task: asyncio.Task | None = None


def _start_or_join_sync() -> asyncio.Task:
    """Return the running sync task, or start one. No await between the
    check and the assignment, so it is atomic on the event loop."""
    global _sync_task
    if _sync_task is None or _sync_task.done():
        _sync_task = asyncio.create_task(_sync_via_subprocess())
    return _sync_task


@router.post("/portfolio/background-sync", status_code=202)
async def portfolio_background_sync():
    """Fire-and-forget portfolio sync. Single-flight: while a sync runs, a new
    trigger returns `in_progress` and spawns nothing."""
    if _sync_task is not None and not _sync_task.done():
        return {"status": "in_progress"}
    _start_or_join_sync()
    return {"status": "accepted"}


async def _sync_via_subprocess() -> ScriptResult:
    """One sync pass, then (XENON_AUDIT_CANCEL=1 only) naked-short audit
    enforcement — both inside the single-flight task. Never raises: the
    shared task may have no awaiter (background trigger)."""
    result = await _run_sync_pass()
    if result.ok and audit_enforce.enabled():
        try:
            await _enforce_naked_short_audit(result.data)
        except Exception:  # noqa: BLE001 — enforcement must never fail the sync
            logger.exception("naked short audit enforcement failed")
    return result


async def _run_sync_pass() -> ScriptResult:
    """Run ib_sync.py as a subprocess with auto-recovery. Never raises."""
    from xenon.api import server
    from xenon.execution import gate4

    try:
        # D5: the child never reads runtime_settings; it gets the effective
        # (env AND DB) netting flag for its naked-short audit in its env.
        # Cached read (TTL 5 s), same as the order guard's orders_paused().
        netting = gate4.netting_enabled()
        result = await server._run_ib_script_with_recovery(
            "xenon-ib-sync",
            ["--sync", "--port", str(DEFAULT_GATEWAY_PORT)],
            timeout=30,
            env={"XENON_GATE4_NETTING": "1" if netting else "0"},
        )
    except Exception as exc:  # noqa: BLE001 — a detached task must log, not raise into the void
        logger.exception("Portfolio sync failed")
        return ScriptResult(ok=False, error=f"portfolio sync failed: {exc}")
    if result.ok:
        logger.info("Portfolio sync complete")
    else:
        logger.error("Portfolio sync failed: %s", result.error)
    return result


async def _enforce_naked_short_audit(data: dict | None):
    """Enforce mode (spec §2.4 Phase B; `services/audit_enforce.py`).

    Cancels go through `_cancel_order`, the core of POST /orders/cancel, on
    the order owner. In-process: the route's read-only and order guards are
    called explicitly (`_audit_cancel_blocked`) before every cancel."""
    if is_read_only():
        logger.warning("XENON_AUDIT_CANCEL=1 ignored: XENON_READ_ONLY=1 never cancels")
        return None
    scope = order_entry._resolve_scope_kwargs()
    outcome = await audit_enforce.enforce(
        audit_enforce.audit_from(data),
        scope_account=scope.get("broker_account") or "",
        owner_client_id=pool_client_id("orders"),
        blocked=_audit_cancel_blocked,
        cancel=_audit_cancel,
        resync=_audit_resync,
    )
    audit_enforce.record_health(outcome, scope)
    return outcome


def _audit_cancel_blocked() -> str | None:
    """Why the audit may not cancel now (None = it may)."""
    from xenon.api import server

    if is_read_only():
        return "read_only"
    if server.order_owner is None:
        return "owner_unavailable"
    if server.order_owner.halted:
        return "owner_halted"
    try:
        denial = ensure_order_allowed("naked-short audit cancel", server.app.state)
    except HTTPException:
        return "mode_unverified"
    if denial is not None:
        return f"order_denied:{order_entry._json_body(denial).get('reason_code')}"
    return None


# The cancel core's refusal for an order that is no longer working
# (`ib_order_manage._cancel_body`), sent before any cancelOrder.
_ALREADY_TERMINAL = re.compile(r"^Order already (Filled|Cancelled|ApiCancelled)\b")


async def _audit_cancel(target: dict) -> str:
    """Cancel one audit target by its full identity; permId is always set,
    so the core never falls back to orderId."""
    kind, resp = await order_entry._cancel_order(
        {"orderId": target["order_id"], "permId": target["perm_id"], "clientId": target["client_id"]},
        reason_code=audit_enforce.REASON_CODE,
    )
    if kind == "ok":
        return audit_enforce.CONFIRMED
    detail = order_entry._json_body(resp).get("detail") or {}
    upstream = detail.get("upstream") if isinstance(detail, dict) else None
    logger.warning("naked short audit cancel %s for %s: %s", kind, target, detail)
    if kind == "failed" and isinstance(upstream, dict) and upstream.get("code") == 10147:
        return audit_enforce.NOT_FOUND  # permId gone: nothing cancelled
    message = str(detail.get("message") or "") if isinstance(detail, dict) else ""
    if kind == "failed" and _ALREADY_TERMINAL.match(message):
        return audit_enforce.GONE  # filled / cancelled before we got there: nothing sent
    return audit_enforce.UNCERTAIN if kind == "uncertain" else audit_enforce.FAILED


async def _audit_resync() -> dict | None:
    result = await _run_sync_pass()
    return audit_enforce.audit_from(result.data) if result.ok else None


async def _futu_refresh_db_first(scope) -> None:
    """Best-effort write path: connect OpenD + sync into Postgres for the scope.

    Bounded + off the event loop: the futu SDK retries a refused OpenD forever, so
    the connect runs in a worker thread under a hard timeout. On timeout/failure we
    log and return — the GET read path serves the last-synced DB rows regardless
    (DB-first). No-ops under XENON_READ_ONLY=1 (sync_futu_orders self-guards).
    """
    import asyncio

    # Catch-up inception for a never-synced scope; thereafter the watermark
    # (max filled_at − lookback) keeps the manual refresh's window tight.
    from datetime import date as _date
    from datetime import datetime, time, timezone

    from xenon.api.services.futu_history_sync import resolve_incremental_since, sync_futu_orders
    from xenon.db.engine import get_engine

    _FUTU_INCEPTION = _date(2024, 1, 1)

    from xenon.api.guards import futu_not_configured

    if futu_not_configured(scope):
        return  # that env has no Futu account: nothing to sync
    client = futu_routes._get_futu_client(scope.account_env)

    def _connect() -> bool:
        if not client.is_connected():
            client.connect()
        return client.is_connected()

    try:
        connected = await asyncio.wait_for(asyncio.to_thread(_connect), timeout=15)
    except Exception as exc:  # TimeoutError, FutuError, etc.
        logger.warning("futu refresh: OpenD connect skipped (%s); serving DB snapshot", exc)
        return
    if not connected:
        return
    try:
        # A manual refresh catches up the gap since the last synced fill (minus a
        # 7-day lookback for late-posted corrections), not just today — so a stack
        # idle for days/weeks returns to current on one click.
        since_date = await resolve_incremental_since(get_engine(), scope, inception=_FUTU_INCEPTION)
        since_dt = datetime.combine(since_date, time.min, tzinfo=timezone.utc)
        await sync_futu_orders(get_engine(), client, scope, since=since_dt)
    except Exception as exc:
        logger.warning("futu refresh: sync failed (%s); serving DB snapshot", exc)


# FUTU /orders/refresh waits at most this long for the observation pass.
FUTU_REFRESH_WAIT_S = 10.0


@router.post("/orders/refresh")
async def orders_refresh(request: Request, scope=Depends(get_broker_scope)):
    """Sync orders into Postgres, then return the scoped payload.

    IB: subprocess pull (auto-allocated clientId 20-49, gateway auto-restart on
    ECONNREFUSED), gated on IB mode verification.
    FUTU: one observation pass (run or joined) of the request scope's own
    loop (`?account_env=`, §11), then the ledger payload (empty with
    `scope_reason` when that scope has no loop or no account). Reads only; the GET read path stays
    Postgres-only.
    """
    from xenon.api import server

    # Read-only sessions get the PG view directly — no --sync subprocess, no
    # OpenD connect, no writes. Placed before the mode check and the test-mode
    # short-circuit so the single contract holds under every flag combination.
    if is_read_only():
        return await asyncio.to_thread(orders_payload_for_scope, scope)

    # IB requires a verified trading mode before any work — including the
    # test-mode short-circuit — so a mode mismatch is refused even in test mode
    # (preserves the /orders/refresh guard contract). FUTU is read-only and
    # carries no IB trading mode to verify, so it is exempt.
    if scope.broker != "FUTU":
        require_mode_verified(request)

    if server._is_test_mode():
        return {"status": "ok", "orders": []}

    if scope.broker == "FUTU":
        # The FUTU read path is the observation ledger, so refresh runs (or
        # joins) one pass of the request scope's own loop only; another
        # scope's pass is never run or waited for. History sync (futu_orders /
        # futu_trades) stays separate (nightly loop, POST /blotter).
        loops = getattr(request.app.state, "futu_observation_loops", None)
        loop = loops.loop_for(scope) if loops is not None else None
        if loop is not None:
            try:
                # Bounded wait (the web client times out first); the pass
                # keeps running and the next read sees its result.
                await asyncio.wait_for(asyncio.shield(loop.refresh()), FUTU_REFRESH_WAIT_S)
            except TimeoutError:
                payload = await asyncio.to_thread(orders_payload_for_scope, scope)
                return {**payload, "refresh": "in_progress"}
        return await asyncio.to_thread(orders_payload_for_scope, scope)

    # IB path — mode already verified above; touch the gateway.
    result = await server._run_ib_script_with_recovery(
        "xenon-ib-orders", ["--sync", "--port", str(DEFAULT_GATEWAY_PORT)], timeout=30
    )
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    return await asyncio.to_thread(orders_payload_for_scope, scope)
