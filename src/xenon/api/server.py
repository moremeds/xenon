"""Xenon FastAPI server — replaces Python shell-outs from Next.js.

Persistent IB connections, shared UW client, uniform JSON responses.
Port 8321, no auth for local use.

Usage:
    python3 -m uvicorn xenon.api.server:app --host 127.0.0.1 --port 8321 --reload
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import func, select

# Project paths — file lives at src/xenon/api/server.py
PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = PROJECT_ROOT / "src"
DATA_DIR = PROJECT_ROOT / "data"
INTERNALS_SKEW_CACHE_DIR = DATA_DIR / "cache"
INTERNALS_SKEW_CACHE_TTL_SECONDS = 60 * 15

from xenon.api import trading_mode
from xenon.api.auth import verify_api_key, verify_clerk_jwt
from xenon.api.guards import get_account_scope, is_read_only, mask_account, read_only_403, require_mode_verified
from xenon.api.ib_gateway import check_ib_gateway, ensure_ib_gateway, is_cloud_mode, is_docker_mode, restart_ib_gateway
from xenon.api.ib_pool import IBPool
from xenon.api.pool_order_manage import pool_cancel_order, pool_modify_order
from xenon.api.routes.historical import router as historical_router
from xenon.api.routes.journal import router as journal_router
from xenon.api.routes.orders import orders_payload_for_scope
from xenon.api.routes.orders import router as orders_router
from xenon.api.routes.performance import router as performance_router
from xenon.api.routes.trades import router as trades_router
from xenon.api.routes.watchlist import router as watchlist_router
from xenon.api.routes.wizard import router as wizard_router
from xenon.api.subprocess import ScriptResult, run_entry_point, run_module
from xenon.api.ws_ticket import create_ticket, validate_ticket
from xenon.clients.ib_client import DEFAULT_GATEWAY_PORT
from xenon.db.engine import dispose_engine, get_sync_engine, init_engine
from xenon.db.queries.blotter import blotter_has_trades, fetch_blotter_pg, merge_pg_and_flex
from xenon.db.schema import account_snapshots, order_events, order_submissions
from xenon.execution import orders_store, preflight, quote_guard, quote_tokens
from xenon.execution.account_scope import resolve_from_app_state
from xenon.execution.preflight import (
    PortfolioView,
    PreflightRequest,
    ReasonCode,
    Verdict,
)
from xenon.execution.quote_tokens import QuotePayload

# Load .env from project root for Python scripts
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv(PROJECT_ROOT / "web" / ".env")
except ImportError:
    pass

logger = logging.getLogger("xenon.api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

# Suppress verbose ib_async logging (positions, orders at INFO level)
logging.getLogger("ib_async").setLevel(logging.WARNING)
logging.getLogger("ib_async.wrapper").setLevel(logging.WARNING)
logging.getLogger("ib_async.client").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
from ib_async import Contract, Index, Option, Stock

from xenon.clients.futu_client import FutuClient
from xenon.clients.futu_exceptions import FutuConnectionError, FutuError

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


# Shared state
# ---------------------------------------------------------------------------
ib_pool: Optional[IBPool] = None
uw_available: bool = False


def _is_test_mode() -> bool:
    # Read at call time so tests that set XENON_API_TEST_MODE after server
    # has been imported (common when other tests imported server first
    # without the flag) still see the flag on.
    return os.environ.get("XENON_API_TEST_MODE", "").lower() in {"1", "true", "yes", "on"}


# Module-level snapshot, kept only for the /health payload. Runtime behavior
# gates MUST call _is_test_mode() — do not branch on this value.
test_mode: bool = _is_test_mode()
test_order_counter: int = 900000


def _next_test_order_ids() -> tuple[int, int]:
    global test_order_counter
    test_order_counter += 1
    order_id = test_order_counter
    perm_id = 8_000_000 + order_id
    return order_id, perm_id


async def _run_rehydrate_on_boot() -> None:
    """F7.2 — reconcile unresolved single-leg orders against IB on boot.

    Blocks boot for up to 10s. On timeout or failure, log and continue —
    serving with a potentially-stale orders view is preferable to refusing
    to boot. Uses the IB pool's ``sync`` role; if the pool has no sync
    client (test mode or gateway down), ``rehydrate_on_boot`` will raise
    and we swallow silently.
    """
    from xenon.execution import orders_store as _orders_store_mod
    from xenon.execution import single_leg_rehydrate as _rehydrate_mod

    def _ib_client_factory():
        if ib_pool is None:
            raise RuntimeError("ib_pool not initialized")
        return ib_pool.get_with_reconnect_sync("sync")

    if _is_test_mode():
        logger.info("test_mode: skipping rehydrate")
        return

    # Resolve scope from app.state populated earlier in lifespan. None values
    # mean "no scope filter" — pre-scope rows still get reconciled.
    _scope_account_env = getattr(app.state, "trading_mode", None)
    _scope_account = getattr(app.state, "account", None)
    _scope_kwargs = {
        "broker": "IB" if _scope_account_env else None,
        "account_env": _scope_account_env,
        "broker_account": _scope_account or None,
    }

    # Operator heartbeat: record "ok" only when BOTH phases finish without
    # exception/timeout. This function swallows phase failures internally, so a
    # bare "ok" after the call would mislabel a failed/timed-out boot as healthy.
    single_ok = combo_ok = False

    try:
        await asyncio.wait_for(
            ib_pool.run_sync(
                "sync",
                _rehydrate_mod.rehydrate_on_boot,
                ib_client_factory=_ib_client_factory,
                orders_store=_orders_store_mod,
                **_scope_kwargs,
            ),
            timeout=10.0,
        )
        single_ok = True
        logger.info("single_leg rehydrate completed on boot")
    except asyncio.TimeoutError:
        logger.warning("single_leg rehydrate timed out after 10s; continuing to serve")
    except Exception as exc:  # noqa: BLE001
        logger.warning("single_leg rehydrate failed on boot; continuing to serve: %s", exc)

    # Combo wizard rehydrate — Task 5.5. Same test-mode guard semantics as
    # single_leg (gated by XENON_ORDERS_DB_PATH when running in test mode).
    try:
        from xenon.execution.combo_wizard import rehydrate as _combo_rehydrate_mod

        await asyncio.wait_for(
            ib_pool.run_sync(
                "sync",
                _combo_rehydrate_mod.rehydrate_combo_sessions,
                ib_client_factory=_ib_client_factory,
                **_scope_kwargs,
            ),
            timeout=10.0,
        )
        combo_ok = True
        logger.info("combo wizard rehydrate completed on boot")
    except asyncio.TimeoutError:
        logger.warning("combo wizard rehydrate timed out after 10s; continuing to serve")
    except Exception as exc:  # noqa: BLE001
        logger.warning("combo wizard rehydrate failed on boot; continuing to serve: %s", exc)

    from xenon.db.service_health import record_service_health

    _ok = single_ok and combo_ok
    record_service_health(
        "ib_rehydrate",
        "ok" if _ok else "error",
        error=None if _ok else {"msg": "rehydrate phase failed/timed out (see logs)"},
        finished_at=datetime.now(timezone.utc),
        broker="IB",
        account_env=_scope_account_env,
        broker_account=_scope_account,
    )


def _maybe_start_activity_poller() -> None:
    """Start the periodic IB→Postgres activity poller if enabled.

    Enabled by default. Set XENON_IB_ACTIVITY_POLLER=0 to disable. The task handle is stored on
    app.state so the lifespan shutdown can cancel + await it cleanly.
    Skipped in test mode and when the IB pool sync role has no client.
    """
    import asyncio as _asyncio

    from xenon.api.services.ib_activity_mirror import (
        DEFAULT_POLL_INTERVAL_S,
        activity_poller_loop,
    )
    from xenon.execution.account_scope import AccountScope

    if _is_test_mode():
        logger.info("test_mode: skipping ib activity poller")
        return

    poller_flag = os.environ.get("XENON_IB_ACTIVITY_POLLER", "").strip().lower()
    if poller_flag in {"0", "false", "no", "off"}:
        logger.info("ib activity poller disabled (XENON_IB_ACTIVITY_POLLER=%s)", poller_flag)
        return

    _scope_account_env = getattr(app.state, "trading_mode", None)
    _scope_account = getattr(app.state, "account", None)
    if not _scope_account_env or not _scope_account:
        logger.info("ib activity poller skipped: scope not resolved")
        return

    scope = AccountScope(
        broker="IB",
        account_env=_scope_account_env,
        broker_account=_scope_account,
    )

    def _ib_client_factory():
        if ib_pool is None:
            raise RuntimeError("ib_pool not initialized")
        return ib_pool.get_with_reconnect_sync("sync")

    async def _sync_role_runner(fn, /, *args, **kwargs):
        """Dispatch each tick onto the sync role's pinned worker so the IB
        client's event loop (set up at connect time) is still current when
        ib_async's internals dispatch awaitables."""
        return await ib_pool.run_sync("sync", fn, *args, **kwargs)

    try:
        interval_s = float(os.environ.get("XENON_IB_ACTIVITY_POLL_S", DEFAULT_POLL_INTERVAL_S))
    except ValueError:
        interval_s = DEFAULT_POLL_INTERVAL_S

    task = _asyncio.create_task(
        activity_poller_loop(
            ib_client_factory=_ib_client_factory,
            scope=scope,
            interval_s=interval_s,
            async_runner=_sync_role_runner,
        )
    )
    app.state.ib_activity_poller_task = task
    logger.info("ib activity poller started: interval=%ss", interval_s)


def _maybe_start_futu_history_loop() -> None:
    """Start the nightly Futu trades + cashflows + NAV walk loop.

    Runs at 16:30 ET every weekday inside the running xenon-api process.
    Idempotent at every layer (UPSERTs on natural keys); a failure on one
    day does not poison the schedule.

    Enabled by default. Set XENON_FUTU_HISTORY_LOOP=0 to disable. Skipped
    in test mode. The task handle lives on app.state for clean shutdown.
    """
    import asyncio as _asyncio

    if _is_test_mode():
        logger.info("test_mode: skipping futu history loop")
        return

    flag = os.environ.get("XENON_FUTU_HISTORY_LOOP", "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        logger.info("futu history loop disabled (XENON_FUTU_HISTORY_LOOP=%s)", flag)
        return

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.info("futu history loop skipped: DATABASE_URL not set")
        return

    from datetime import date as _date

    from sqlalchemy.ext.asyncio import create_async_engine as _create_engine

    from xenon.api.services.futu_history_scheduler import futu_history_loop
    from xenon.cli.futu_history_sync import run_history_sync
    from xenon.execution.account_scope import AccountScope

    def _engine_factory():
        return _create_engine(db_url, pool_pre_ping=True)

    def _scope_factory():
        client = _get_futu_client()
        if not client.is_connected():
            client.connect()
        matched_env = (client._matched_trd_env or client.trd_env or "REAL").upper()
        account_env = {"REAL": "live", "SIMULATE": "paper"}.get(matched_env, "paper")
        return AccountScope(
            broker="FUTU",
            account_env=account_env,
            broker_account=str(client._acc_id),
        )

    task = _asyncio.create_task(
        futu_history_loop(
            engine_factory=_engine_factory,
            scope_factory=_scope_factory,
            runner=run_history_sync,
        )
    )
    app.state.futu_history_loop_task = task
    logger.info("futu history loop started (target: 16:30 ET weekdays)")


async def _run_fills_replay_on_boot() -> None:
    """Replay IB fills into xenon.order_fills once on boot. Best-effort.

    Mirrors the open-order import that PR #67 added on the order side.
    Without this, fills from TWS-placed or TWS-modified orders are
    invisible to the blotter (xenon.trades is derived from
    xenon.order_fills, which is only written by the in-process placement
    flow). The standalone CLI ``xenon-ib-reconcile`` does the same job
    but no scheduler runs it — operators forget.

    Skipped in test mode and when the IB pool sync role has no client
    (gateway down). 30s timeout — boot must not hang on a slow IB.
    """
    from xenon.api.services.ib_activity_mirror import reconcile_fills_on_boot
    from xenon.execution.account_scope import AccountScope

    if _is_test_mode():
        logger.info("test_mode: skipping ib fills replay")
        return

    def _ib_client_factory():
        if ib_pool is None:
            raise RuntimeError("ib_pool not initialized")
        return ib_pool.get_with_reconnect_sync("sync")

    _scope_account_env = getattr(app.state, "trading_mode", None)
    _scope_account = getattr(app.state, "account", None)
    if not _scope_account_env or not _scope_account:
        logger.info("ib fills replay skipped: scope not resolved (trading_mode/account empty)")
        return

    scope = AccountScope(
        broker="IB",
        account_env=_scope_account_env,
        broker_account=_scope_account,
    )

    from xenon.db.service_health import record_service_health

    def _hb(state: str, err: dict | None = None) -> None:
        record_service_health(
            "ib_fills_replay",
            state,
            error=err,
            finished_at=datetime.now(timezone.utc),
            broker="IB",
            account_env=_scope_account_env,
            broker_account=_scope_account,
        )

    try:
        result = await asyncio.wait_for(
            ib_pool.run_sync(
                "sync",
                reconcile_fills_on_boot,
                ib_client_factory=_ib_client_factory,
                scope=scope,
            ),
            timeout=30.0,
        )
        # Derive heartbeat state from the actual result — reconcile_fills_on_boot
        # returns {"skipped": ...}/{"error": ...} WITHOUT raising, so a bare "ok"
        # after the await would mislabel skipped/failed boots as healthy.
        r = result or {}
        if r.get("error"):
            _hb("error", {"msg": str(r.get("error"))})
        elif r.get("skipped"):
            _hb("paused", {"msg": str(r.get("skipped"))})
        else:
            _hb("ok")
    except asyncio.TimeoutError:
        logger.warning("ib fills replay timed out after 30s; continuing to serve")
        _hb("error", {"msg": "timed out after 30s"})
    except Exception as exc:  # noqa: BLE001
        logger.warning("ib fills replay failed on boot; continuing to serve: %s", exc)
        _hb("error", {"msg": str(exc)})


def _get_managed_account_for_health() -> str:
    """Return the first managedAccount from the IB pool's sync client.

    Returns "" when the pool isn't connected (test mode, Gateway down, etc.).
    Pulled out as a module-level function so tests can monkeypatch it
    without booting a real IB connection.
    """
    if ib_pool is None:
        return ""
    client = ib_pool.get("sync")
    if client is None:
        return ""
    try:
        accounts = client.ib.managedAccounts()
    except Exception:  # noqa: BLE001
        return ""
    return accounts[0] if accounts else ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start IB pool and UW client on startup, tear down on shutdown."""
    global ib_pool, uw_available

    if _is_test_mode():
        logger.info("Xenon API starting in test mode; IB Gateway and pool startup are disabled")
        uw_available = bool(os.environ.get("UW_TOKEN"))
        orders_store.init_store()
        if os.environ.get("DATABASE_URL"):
            app.state.db_engine = init_engine()
        await _run_rehydrate_on_boot()
        app.state.trading_mode = trading_mode.MODE
        # Tests monkeypatch _get_managed_account_for_health; honor that.
        account = _get_managed_account_for_health()
        app.state.account = account
        app.state.mode_verified = trading_mode.verify_account(account)
        yield
        logger.info("Xenon API test mode shut down")
        return

    # Ensure IB Gateway is running before connecting pool
    gw_status = await ensure_ib_gateway()
    logger.info("IB Gateway: %s", gw_status)

    # IB pool — starts degraded if Gateway is still down after restart attempt
    ib_pool = IBPool()
    app.state.ib_pool = ib_pool
    pool_status = await ib_pool.connect_all()
    logger.info("IB pool status: %s", pool_status)

    # Trading-mode prefix guard — verify Gateway login matches XENON_TRADING_MODE.
    # Failure does not abort startup; it sets app.state.mode_verified=False
    # and the order routes refuse to serve until .env + Gateway are aligned.
    account = await asyncio.to_thread(_get_managed_account_for_health)
    verified = trading_mode.verify_account(account)
    app.state.trading_mode = trading_mode.MODE
    app.state.account = account
    app.state.mode_verified = verified
    if not verified:
        logger.error(
            "TRADING MODE MISMATCH — declared=%s, account=%r (expected prefix %r). "
            "Order routes will return 503 until .env XENON_TRADING_MODE matches "
            "the IB Gateway login.",
            trading_mode.MODE,
            account,
            trading_mode.EXPECTED_PREFIX,
        )
    else:
        logger.info("Trading mode verified: %s account=%s", trading_mode.MODE, account)

    orders_store.init_store()
    app.state.db_engine = init_engine()

    # UW client — just verify token exists
    uw_available = bool(os.environ.get("UW_TOKEN"))
    if not uw_available:
        logger.warning("UW_TOKEN not set — UW-dependent endpoints will fail")

    # Read-only mode (set by `dev.sh live`) blocks every persistence path
    # below: rehydrate writes new rows to xenon.order_submissions; the
    # fills-replay and activity poller both write to xenon.order_fills
    # and xenon.trades. In read-only sessions the live IB connection is
    # still up so the dev workflow can inspect TWS-side state, but
    # nothing lands in core_test.
    if is_read_only():
        logger.warning("XENON_READ_ONLY=1 — skipping rehydrate, fills replay, and activity poller (no PG writes).")
    else:
        # F7.2 — single-leg three-source rehydrate. Runs synchronously before
        # the server starts serving so our view of in-flight orders is accurate
        # on first request. Failures are logged + swallowed so boot cannot be
        # blocked by a transient IB hiccup. Known limitation: positions_changed
        # heuristic has no persisted baseline on boot, so unknowns map to
        # UNKNOWN rather than auto-CANCELLED (per F7.1 design).
        await _run_rehydrate_on_boot()

        # IB→Postgres activity mirror — boot replay of fills. Pulls executions
        # from the same long-lived IB sync client and inserts new ones into
        # xenon.order_fills + aggregated xenon.trades. Best-effort: any failure
        # is logged and swallowed. Without this, fills from TWS-modified or
        # TWS-placed orders never reach the blotter unless an operator runs
        # `xenon-ib-reconcile` manually.
        await _run_fills_replay_on_boot()

        # Periodic IB activity poller. Mirrors TWS-side activity (price/qty
        # edits, new fills, cancels expressed by disappearance) into Postgres
        # on a fixed cadence. Enabled by default; set XENON_IB_ACTIVITY_POLLER=0
        # to suppress. Cadence env: XENON_IB_ACTIVITY_POLL_S.
        _maybe_start_activity_poller()

    # Nightly Futu trades + cashflows + NAV walk. Runs at 16:30 ET every
    # weekday. Idempotent via UPSERT on natural keys. Enabled by default;
    # set XENON_FUTU_HISTORY_LOOP=0 to suppress.
    _maybe_start_futu_history_loop()

    # W4.7 — PG-event-driven journal auto-import listener.
    # Replaces the legacy periodic /journal/sync flow. Failures must not
    # block boot.
    try:
        from xenon.api.services.journal_auto_import import JournalAutoImportSubscriber

        journal_auto_import = JournalAutoImportSubscriber()
        await journal_auto_import.start()
        app.state.journal_auto_import = journal_auto_import
    except Exception:  # noqa: BLE001
        logger.exception("journal auto-import listener failed to start")

    try:
        yield
    finally:
        # Shutdown — always runs, even if the app raised.
        listener = getattr(app.state, "journal_auto_import", None)
        if listener is not None:
            try:
                await listener.stop()
            except Exception:  # noqa: BLE001
                logger.exception("journal auto-import listener failed to stop")
        ib_activity_task = getattr(app.state, "ib_activity_poller_task", None)
        if ib_activity_task is not None:
            ib_activity_task.cancel()
            try:
                await ib_activity_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        futu_loop_task = getattr(app.state, "futu_history_loop_task", None)
        if futu_loop_task is not None:
            futu_loop_task.cancel()
            try:
                await futu_loop_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if ib_pool:
            try:
                await ib_pool.disconnect_all()
            except Exception as exc:  # noqa: BLE001
                logger.warning("ib_pool.disconnect_all failed: %s", exc)
        if _futu_client is not None:
            try:
                _futu_client.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.warning("FutuClient disconnect on shutdown failed: %s", exc)
        await dispose_engine()
        logger.info("Xenon API shut down")


app = FastAPI(title="Xenon API", version="1.0.0", lifespan=lifespan)
app.include_router(historical_router)
app.include_router(journal_router)
app.include_router(orders_router)
app.include_router(performance_router)
app.include_router(trades_router)
app.include_router(watchlist_router)
app.include_router(wizard_router)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://localhost:3000|http://127\.0\.0\.1:3000",
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth middleware — protect all routes except /health and internal ticket validation
AUTH_EXEMPT_PATHS = {
    "/health",
    "/ws-ticket/validate",
    "/docs",
    "/openapi.json",
    # B3 — /dev/rehydrate/synthetic intentionally NOT listed here. The route
    # is protected by the _dev_probes_enabled() gate (DEV_PROBES/test_mode),
    # the localhost-bypass below, and standard Clerk auth. Exempting it would
    # expose a public write endpoint if DEV_PROBES=1 leaked into a non-local env.
}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Require Clerk JWT for all endpoints except exempted paths and localhost."""
    if request.url.path in AUTH_EXEMPT_PATHS:
        return await call_next(request)

    if not os.environ.get("CLERK_JWKS_URL"):
        return await call_next(request)

    # Skip auth for server-to-server calls from localhost (Next.js → FastAPI)
    client_host = request.client.host if request.client else None
    if client_host in ("127.0.0.1", "::1"):
        return await call_next(request)

    # API key auth — scoped to historical/contract endpoints only
    service_identity = verify_api_key(request)
    if service_identity:
        request.state.user = service_identity
        return await call_next(request)

    try:
        payload = await verify_clerk_jwt(request)
        request.state.user = payload
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    return await call_next(request)


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


_SCAN_TYPE_MAP = {
    "scanner.json": "watchlist",
    "discover.json": "discover",
    "gex.json": "gex",
    "vcg.json": "vcg",
    "cri.json": "cri",
}


def _atomic_save(path: str, data: dict) -> str:
    """Use the project's atomic_save for portfolio/orders files."""
    from xenon.utils.atomic_io import atomic_save

    return atomic_save(path, data)


def _coerce_float(value: object) -> Optional[float]:
    """Parse an arbitrary value into a finite float."""
    if isinstance(value, (int, float)):
        return float(value) if value == value and value != float("inf") and value != float("-inf") else None
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None
    return None


def _coerce_date(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None


def _pick_preferred_expiry(raw: object, now: Optional[datetime] = None) -> Optional[str]:
    """Choose the nearest expiry that is today or newer, else the most recent expiry."""
    candidates = _extract_expiry_candidates(raw)
    if not candidates:
        return None

    parsed: List[Tuple[str, datetime]] = []
    for expiry in candidates:
        parsed_date = _coerce_date(expiry)
        if parsed_date is None:
            continue
        parsed.append((expiry, parsed_date))

    if not parsed:
        return candidates[0]

    current = now or datetime.now(timezone.utc)
    future_candidates = [
        (expiry, expiry_date) for expiry, expiry_date in parsed if expiry_date.date() >= current.date()
    ]
    if future_candidates:
        return min(future_candidates, key=lambda item: item[1])[0]
    return max(parsed, key=lambda item: item[1])[0]


def _extract_ib_expiry_candidates(raw: object) -> List[str]:
    rows: Iterable[object] = raw if isinstance(raw, list) else []
    candidates: List[str] = []
    for row in rows:
        expirations = getattr(row, "expirations", None)
        if not expirations:
            continue
        for expiry in expirations:
            normalized = _normalize_expiry_string(expiry)
            if normalized and normalized not in candidates:
                candidates.append(normalized)
    return candidates


async def _fetch_ib_expiry_candidates(ticker: str) -> List[str]:
    normalized_ticker = ticker.upper()
    if ib_pool is None:
        return []

    attempts = [
        ("NASDAQ", "IND"),
        ("CBOE", "IND"),
        ("SMART", "IND"),
        ("", "IND"),
    ]
    for exchange, sec_type in attempts:
        try:
            async with ib_pool.acquire("data") as client:
                chains = await ib_pool.run_sync(
                    "data",
                    _fetch_ib_index_option_chain,
                    client,
                    normalized_ticker,
                    exchange,
                    sec_type,
                )
            candidates = _sort_expiry_candidates(_extract_ib_expiry_candidates(chains))
            if candidates:
                logger.info(
                    "Internals skew: IB expiries for %s resolved via %s/%s (%d candidates)",
                    normalized_ticker,
                    exchange or "default",
                    sec_type,
                    len(candidates),
                )
                return candidates
        except Exception as exc:
            logger.warning(
                "Internals skew: IB expiry lookup failed for %s via %s/%s: %s",
                normalized_ticker,
                exchange or "default",
                sec_type,
                exc,
            )
    return []


def _preferred_index_exchange(ticker: str) -> str:
    return "NASDAQ" if ticker.upper() == "NDX" else "CBOE"


def _fetch_ib_index_option_chain(client: Any, ticker: str, exchange: str, sec_type: str) -> object:
    if sec_type != "IND":
        return client.get_option_chain(ticker, exchange, sec_type)

    contract = Index(symbol=ticker, exchange=exchange or _preferred_index_exchange(ticker))
    qualified = client.qualify_contract(contract)
    return client.ib.reqSecDefOptParams(ticker, exchange, sec_type, qualified.conId)


def _series_span_days(rows: List[dict]) -> int:
    if len(rows) < 2:
        return 0
    start = _coerce_date(rows[0].get("date"))
    end = _coerce_date(rows[-1].get("date"))
    if start is None or end is None:
        return 0
    return (end.date() - start.date()).days


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def _compute_futu_health() -> Dict[str, Any]:
    """Derive the Futu health block from the singleton + cached file.

    Never probes OpenD (tribunal T11) — reachability comes from the last
    cached sync result, not a TCP probe. Cheap enough to compute per
    /health call.
    """
    cached_path = DATA_DIR / "futu_portfolio.json"
    last_sync_at: Optional[str] = None
    last_sync_age_s: Optional[float] = None
    if cached_path.exists():
        try:
            data = json.loads(cached_path.read_text())
            last_sync_at = data.get("fetched_at")
            if last_sync_at:
                from datetime import datetime, timezone

                dt = datetime.fromisoformat(last_sync_at.replace("Z", "+00:00"))
                last_sync_age_s = (datetime.now(timezone.utc) - dt).total_seconds()
        except Exception:
            pass

    return {
        "configured": True,  # v1 assumption; cloud branch flips to False
        "connected": _futu_client is not None and _futu_client.is_connected(),
        "last_sync_at": last_sync_at,
        "last_sync_age_s": last_sync_age_s,
    }


def _iso_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _snapshotter_health(scope: dict | None = None) -> dict:
    try:
        engine = get_sync_engine()
        with engine.connect() as conn:
            stmt = select(func.max(account_snapshots.c.snapshot_at))
            if scope is not None:
                stmt = stmt.where(
                    account_snapshots.c.broker == scope["broker"],
                    account_snapshots.c.account_env == scope["account_env"],
                    account_snapshots.c.broker_account == scope["broker_account"],
                )
            last_write_at = conn.execute(stmt).scalar()
    except Exception:
        logger.warning("[health] failed to load snapshotter heartbeat", exc_info=True)
        return {"last_write_at": None, "stale_seconds": None}

    if last_write_at is None:
        return {"last_write_at": None, "stale_seconds": None}
    if last_write_at.tzinfo is None:
        last_write_at = last_write_at.replace(tzinfo=timezone.utc)
    stale_seconds = max(0, int((datetime.now(timezone.utc) - last_write_at).total_seconds()))
    return {"last_write_at": _iso_datetime(last_write_at), "stale_seconds": stale_seconds}


def _order_submissions_health(scope: dict | None = None) -> dict:
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
        engine = get_sync_engine()
        with engine.connect() as conn:
            stmt = (
                select(func.count())
                .select_from(order_submissions)
                .where(
                    order_submissions.c.state == "UNKNOWN",
                    order_submissions.c.submitted_at >= cutoff,
                )
            )
            if scope is not None:
                stmt = stmt.where(
                    order_submissions.c.broker == scope["broker"],
                    order_submissions.c.account_env == scope["account_env"],
                    order_submissions.c.broker_account == scope["broker_account"],
                )
            unknown_count = conn.execute(stmt).scalar()
    except Exception:
        logger.warning("[health] failed to load order submissions health", exc_info=True)
        return {"unknown_count": None, "alarm": False}

    count = int(unknown_count or 0)
    return {"unknown_count": count, "alarm": count > 5}


def _flex_divergence_health() -> dict:
    """Latest nightly PG↔Flex divergence run, if any. Resolves scope via app.state."""
    try:
        from xenon.execution.account_scope import AccountScope
        from xenon.jobs.flex_divergence_check import latest_run

        kwargs = _resolve_scope_kwargs()
        scope = AccountScope(
            broker=kwargs["broker"],
            account_env=kwargs["account_env"],
            broker_account=kwargs["broker_account"],
        )
        row = latest_run(scope=scope)
    except Exception:  # noqa: BLE001
        logger.warning("[health] failed to load flex_divergence", exc_info=True)
        return {"configured": False}
    if row is None:
        return {"configured": True, "ran_at": None, "divergence_count": None, "total_compared": None}
    return {
        "configured": True,
        "ran_at": _iso_datetime(row.get("ran_at")),
        "total_compared": row["total_compared"],
        "divergence_count": row["divergence_count"],
    }


def _resolve_realtime_port() -> int:
    """Resolve the IB realtime WS port from the runtime file, else 8765.

    Mirror of web/lib/server/ibRealtimeRuntime.ts: IB_REALTIME_RUNTIME_FILE env,
    else <tmpdir>/xenon-ib-realtime.json; fall back to 8765 when absent/invalid.
    """
    import tempfile
    from pathlib import Path

    runtime_file = os.environ.get("IB_REALTIME_RUNTIME_FILE") or str(
        Path(tempfile.gettempdir()) / "xenon-ib-realtime.json"
    )
    try:
        data = json.loads(Path(runtime_file).read_text())
        port = int(data.get("port"))
        if port > 0:
            return port
    except Exception:  # noqa: BLE001
        pass
    return 8765


def _resolve_realtime_status_url() -> str:
    """Realtime /status URL. Explicit IB_REALTIME_STATUS_URL wins (prod cross-
    container, e.g. http://realtime:8765/status); else loopback on the resolved
    port (single-host dev, where /status is reachable on 127.0.0.1)."""
    explicit = os.environ.get("IB_REALTIME_STATUS_URL")
    if explicit:
        return explicit
    return f"http://127.0.0.1:{_resolve_realtime_port()}/status"


def _fetch_realtime_status_json(url: str, timeout: float = 0.5) -> dict:
    import urllib.request

    headers = {}
    token = os.environ.get("IB_REALTIME_STATUS_TOKEN")
    if token:
        headers["X-Status-Token"] = token
    request = urllib.request.Request(url, headers=headers)  # noqa: S310
    with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _realtime_subscribers_health() -> dict:
    """Realtime WS subscriber health, silent-degrade when the server is down."""
    try:
        payload = _fetch_realtime_status_json(_resolve_realtime_status_url())
    except Exception:  # noqa: BLE001
        logger.warning("[health] realtime /status unreachable", exc_info=True)
        return {"reachable": False, "subscribers": [], "anonymous_count": 0}
    return {
        "reachable": True,
        "ib_connected": payload.get("ib_connected"),
        "subscribers": payload.get("subscribers", []),
        "anonymous_count": payload.get("anonymous_count", 0),
        "ttl_ms": payload.get("ttl_ms"),
    }


# --- Operator console helpers (Tier A/B reads) ---------------------------

# Background writers the Operator console expects to see. Missing ones are
# synthesized as state="missing" so a never-started writer is visible, not absent.
EXPECTED_WRITERS = (
    "ib_activity_poller",
    "ib_fills_replay",
    "ib_rehydrate",
    "futu_history",
    "naked_short_audit",
)


def _ib_auth_verdict(gw: dict, pool: dict) -> str:
    """Derive an IB auth verdict from gateway + pool status."""
    if not gw.get("port_listening"):
        return "unreachable"
    if gw.get("upstream_dead"):
        return "awaiting"
    any_connected = any((r or {}).get("connected") for r in pool.values()) if pool else False
    return "authenticated" if any_connected else "unknown"


def _service_health_rows() -> list[dict]:
    """service_health rows for the active scope, sorted by service, with
    age_secs + synthesized 'missing' rows for expected writers with no row.

    futu_history is matched by service name across scopes (it runs under the
    FUTU account, not the active IB scope); the latest row wins."""
    try:
        from sqlalchemy import and_, or_

        from xenon.db.schema import service_health

        kwargs = _resolve_scope_kwargs()
        engine = get_sync_engine()
        with engine.connect() as conn:
            rows = (
                conn.execute(
                    select(service_health)
                    .where(
                        or_(
                            and_(
                                service_health.c.broker == kwargs["broker"],
                                service_health.c.account_env == kwargs["account_env"],
                                service_health.c.broker_account == kwargs["broker_account"],
                            ),
                            # futu_history runs under the FUTU account scope, not
                            # the active IB scope — match it by service name across
                            # scopes (latest row wins below) so it is not forever
                            # synthesized as "missing".
                            service_health.c.service == "futu_history",
                        )
                    )
                    .order_by(
                        service_health.c.service,
                        service_health.c.updated_at.desc(),
                    )
                )
                .mappings()
                .all()
            )
    except Exception:
        logger.warning("[operator] failed to load service_health", exc_info=True)
        rows = []
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        if r["service"] in seen:
            # ordered updated_at DESC within service → first row is the latest;
            # skip older duplicates (e.g. prod+dev futu_history after the refresh).
            continue
        seen.add(r["service"])
        updated = r["updated_at"]
        if updated is not None and updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        out.append(
            {
                "service": r["service"],
                "state": r["state"],
                "detail": r["detail"],
                "last_error": r["last_error"],
                "last_started_at": _iso_datetime(r["last_started_at"]),
                "last_finished_at": _iso_datetime(r["last_finished_at"]),
                "updated_at": _iso_datetime(updated),
                "age_secs": int((now - updated).total_seconds()) if updated else None,
            }
        )
    for svc in EXPECTED_WRITERS:
        if svc not in seen:
            out.append(
                {
                    "service": svc,
                    "state": "missing",
                    "detail": None,
                    "last_error": None,
                    "last_started_at": None,
                    "last_finished_at": None,
                    "updated_at": None,
                    "age_secs": None,
                }
            )
    out.sort(key=lambda r: r["service"])
    return out


@app.get("/health")
async def health():
    gw = await check_ib_gateway()
    return {
        "status": "ok",
        "test_mode": _is_test_mode(),
        "ib_gateway": gw,
        "ib_pool": ib_pool.status() if ib_pool else {},
        "uw": uw_available,
        "futu": _compute_futu_health(),
        "trading_mode": getattr(app.state, "trading_mode", trading_mode.MODE),
        # /health is auth-exempt — mask the IB account so the public payload
        # does not leak the full identifier. The raw value stays on app.state
        # for the require_mode_verified 503 detail (which is auth-gated).
        "account": mask_account(getattr(app.state, "account", "")),
        "mode_verified": getattr(app.state, "mode_verified", False),
        "snapshotter": _snapshotter_health(),
        "order_submissions": _order_submissions_health(),
        "flex_divergence": _flex_divergence_health(),
        "realtime_subscribers": await asyncio.to_thread(_realtime_subscribers_health),
    }


@app.get("/admin/operator")
async def admin_operator():
    """Operator console aggregate. Read-only. No per-route Depends — gated by
    the global auth_middleware like every data route (a per-route Depends would
    401 cross-container in Docker since the Next proxy forwards no token). The
    DB helpers are called inline (not via to_thread) to match /health and to
    stay on the Phase-2 test connection."""
    gw = await check_ib_gateway()
    pool = ib_pool.status() if ib_pool else {}
    scope = _resolve_scope_kwargs()
    return {
        "generated_at": _iso_datetime(datetime.now(timezone.utc)),
        "ib_gateway": gw,
        "ib_pool": pool,
        "ib_auth": _ib_auth_verdict(gw, pool),
        "trading_mode": getattr(app.state, "trading_mode", trading_mode.MODE),
        "account": mask_account(getattr(app.state, "account", "")),
        "mode_verified": getattr(app.state, "mode_verified", False),
        "snapshotter": _snapshotter_health(scope),
        "order_submissions": _order_submissions_health(scope),
        "flex_divergence": _flex_divergence_health(),
        "realtime_subscribers": await asyncio.to_thread(_realtime_subscribers_health),
        "futu": _compute_futu_health(),
        "writers": _service_health_rows(),
    }


def _dev_probes_enabled() -> bool:
    """True iff test_mode is on OR DEV_PROBES=1 is set in the environment."""
    return _is_test_mode() or os.environ.get("DEV_PROBES", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


class _FakeEmptyIBClient:
    """Stand-in IB client with no open orders / executions / positions.

    Used by the synthetic rehydrate probe so a backdated PENDING row
    deterministically reconciles to FAILED/PENDING_TIMEOUT.
    """

    def get_open_orders(self):
        return []

    def get_executions(self):
        return []

    def get_positions(self):
        return {}


@app.post("/dev/rehydrate/synthetic", include_in_schema=False)
async def dev_rehydrate_synthetic():
    """Inject a synthetic PENDING row, run rehydrate_on_boot, return event count.

    Dev-only. Verifies the rehydrate path end-to-end (reservation → backdate →
    reconcile → orders_events) without needing a live IB Gateway. Used as a
    burn-in / observability readiness check.
    """
    if not _dev_probes_enabled():
        raise HTTPException(status_code=404)

    import uuid as _uuid
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    from datetime import timezone as _tz
    from decimal import Decimal as _Dec

    from xenon.execution import orders_store as _orders_store_mod
    from xenon.execution import single_leg_rehydrate as _rehydrate_mod
    from xenon.execution.single_leg_rehydrate import PENDING_TIMEOUT_SECONDS

    client_attempt_id = f"synthetic-{_uuid.uuid4()}"
    reservation = _orders_store_mod.reserve_attempt(
        user_id="dev-probe",
        client_attempt_id=client_attempt_id,
        request=_orders_store_mod.RequestRow(
            ticker="SPY",
            security_type="STK",
            action="BUY",
            quantity=1,
            multiplier=1,
            limit_price=_Dec("500"),
        ),
        **_resolve_scope_kwargs(),
    )
    submission_id = reservation.submission_id

    # Backdate submitted_at past the PENDING timeout so the reconcile decision
    # is deterministic (FAILED / PENDING_TIMEOUT). Inline UPDATE — probe-specific.
    # Use the UTC-session helper so the naive round-trip preserves the epoch.
    backdated = _dt.now(_tz.utc) - _td(seconds=PENDING_TIMEOUT_SECONDS + 5)
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            order_submissions.update()
            .where(order_submissions.c.submission_id == submission_id)
            .values(submitted_at=backdated)
        )

    decisions = await asyncio.to_thread(
        _rehydrate_mod.rehydrate_on_boot,
        ib_client_factory=lambda: _FakeEmptyIBClient(),
        orders_store=_orders_store_mod,
    )

    # Count events written for this submission
    with engine.connect() as conn:
        events_count = int(
            conn.execute(
                select(func.count()).select_from(order_events).where(order_events.c.submission_id == submission_id)
            ).scalar()
            or 0
        )

    summary = [
        {
            "to_state": d.to_state,
            "reason_code": d.reason_code,
            "event_kind": d.event_kind,
        }
        for d in decisions
    ]

    return {
        "submission_id": submission_id,
        "events_added": events_count,
        "decisions": summary,
    }


@app.post("/ws-ticket")
async def get_ws_ticket(payload: dict = Depends(verify_clerk_jwt)):
    """Issue a short-lived ticket for WebSocket authentication."""
    ticket = create_ticket(payload["sub"])
    return {"ticket": ticket}


@app.post("/ws-ticket/validate")
async def validate_ws_ticket(request: Request):
    """Validate a WebSocket ticket (called by the Node.js relay). Internal only."""
    body = await request.json()
    ticket = body.get("ticket", "")
    user_id = validate_ticket(ticket)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired ticket")
    return {"user_id": user_id}


@app.post("/ib/restart")
async def ib_restart():
    """Restart IB Gateway via IBC service, then reconnect pool."""
    result = await restart_ib_gateway()
    if not result["restarted"]:
        raise HTTPException(status_code=503, detail=result.get("error", "Restart failed"))

    # Reconnect pool after Gateway restart
    if ib_pool:
        await ib_pool.disconnect_all()
        pool_status = await ib_pool.connect_all()
        result["pool"] = pool_status

    return result


# ---------------------------------------------------------------------------
# Phase 1: Stateless UW-only endpoints (subprocess-based)
# ---------------------------------------------------------------------------
def _empty_discover_payload() -> dict:
    return {
        "discovery_time": "",
        "alerts_analyzed": 0,
        "candidates_found": 0,
        "candidates": [],
    }


def _empty_cri_payload() -> dict:
    return {
        "scan_time": "",
        "date": "",
        "market_open": _is_market_open_now(),
        "vix": None,
        "vvix": None,
        "spy": None,
        "cri": {"score": 0, "level": "LOW", "components": {}},
        "cta": {},
        "menthorq_cta": None,
        "crash_trigger": {"triggered": False, "conditions": {}, "values": {}},
        "history": [],
        "spy_closes": [],
    }


@app.get("/attribution")
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


@app.get("/portfolio")
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


@app.post("/portfolio/sync")
async def portfolio_sync(scope=Depends(get_account_scope)):
    """Sync portfolio from IB via subprocess, then return the fresh payload.

    Scripts auto-allocate client IDs from subprocess range (20-49).
    Auto-restarts IB Gateway on ECONNREFUSED and retries once. Reads the
    just-written snapshot from Postgres (no longer hits data/portfolio.json).
    """
    result = await _run_ib_script_with_recovery(
        "xenon-ib-sync", ["--sync", "--port", str(DEFAULT_GATEWAY_PORT)], timeout=30
    )
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    payload = await _read_portfolio_payload(scope)
    if payload is None:
        raise HTTPException(
            status_code=502,
            detail="ib_sync ran but no snapshot landed in Postgres — check ib_sync logs.",
        )
    return payload


@app.post("/portfolio/background-sync", status_code=202)
async def portfolio_background_sync(bg: BackgroundTasks):
    """Fire-and-forget portfolio sync."""
    bg.add_task(_bg_sync_via_subprocess)
    return {"status": "accepted"}


async def _bg_sync_via_subprocess():
    """Background task: run ib_sync.py as subprocess with auto-recovery."""
    result = await _run_ib_script_with_recovery(
        "xenon-ib-sync", ["--sync", "--port", str(DEFAULT_GATEWAY_PORT)], timeout=30
    )
    if result.ok:
        logger.info("Background portfolio sync complete")
    else:
        logger.error("Background portfolio sync failed: %s", result.error)


@app.post("/orders/refresh", dependencies=[Depends(require_mode_verified)])
async def orders_refresh(scope=Depends(get_account_scope)):
    """Sync orders from IB via subprocess.

    Scripts auto-allocate client IDs from subprocess range (20-49).
    Auto-restarts IB Gateway on ECONNREFUSED and retries once.
    """
    if _is_test_mode():
        return {"status": "ok", "orders": []}

    result = await _run_ib_script_with_recovery(
        "xenon-ib-orders", ["--sync", "--port", str(DEFAULT_GATEWAY_PORT)], timeout=30
    )
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    return orders_payload_for_scope(scope)


# ---------------------------------------------------------------------------
# Phase 3: IB order operations
# ---------------------------------------------------------------------------


def _load_portfolio_view_sync() -> tuple[PortfolioView | None, datetime | None]:
    """Load the latest scoped portfolio snapshot from Postgres for preflight."""
    scope = _resolve_scope_kwargs()
    try:
        engine = get_sync_engine()
        stmt = (
            select(account_snapshots.c.payload, account_snapshots.c.snapshot_at)
            .where(account_snapshots.c.broker == scope["broker"])
            .where(account_snapshots.c.account_env == scope["account_env"])
            .where(account_snapshots.c.broker_account == scope["broker_account"])
            .order_by(account_snapshots.c.snapshot_at.desc())
            .limit(1)
        )
        with engine.connect() as con:
            row = con.execute(stmt).first()
        if row is None or not row.payload:
            return None, None
        return PortfolioView.model_validate(dict(row.payload)), row.snapshot_at
    except (ValueError, ValidationError) as exc:
        logger.warning("[preflight] Could not validate Postgres portfolio snapshot: %s", exc)
        return None, None
    except Exception as exc:
        logger.warning("[preflight] Could not load Postgres portfolio snapshot: %s", exc)
        return None, None


async def _load_portfolio_view() -> tuple[PortfolioView | None, datetime | None]:
    return await asyncio.to_thread(_load_portfolio_view_sync)


def _unpack_portfolio_load(loaded) -> tuple[PortfolioView | None, datetime | None]:
    if isinstance(loaded, tuple) and len(loaded) == 2:
        return loaded
    return loaded, None


def _portfolio_snapshot_stale_response(snapshot_at: datetime | None) -> Verdict | None:
    if snapshot_at is None:
        return None
    if snapshot_at.tzinfo is None:
        snapshot_at = snapshot_at.replace(tzinfo=timezone.utc)
    market_open = _is_market_open_now()
    threshold_env = "XENON_PORTFOLIO_SNAPSHOT_STALE_S" if market_open else "XENON_PORTFOLIO_SNAPSHOT_STALE_CLOSED_S"
    default_seconds = "300" if market_open else "1800"
    try:
        threshold_seconds = int(os.environ.get(threshold_env, default_seconds))
    except ValueError:
        threshold_seconds = int(default_seconds)
    age_seconds = (datetime.now(timezone.utc) - snapshot_at).total_seconds()
    if age_seconds <= threshold_seconds:
        return None
    return Verdict(
        accept=False,
        reason_code=ReasonCode.PORTFOLIO_SNAPSHOT_STALE,
        reason_detail=(
            f"Portfolio snapshot is stale ({int(age_seconds)}s old; "
            f"threshold {threshold_seconds}s). Sync portfolio before submitting SELL exposure."
        ),
    )


def _body_to_preflight_request(body: dict) -> PreflightRequest:
    """Translate non-combo /orders/place bodies to PreflightRequest."""
    from xenon.execution.universe import UNIVERSE, get_multiplier

    sec_type = "STK" if body.get("type") == "stock" else "OPT"
    right_raw = (body.get("right") or "").upper()
    right = right_raw if right_raw in ("C", "P") else None
    limit = body.get("limitPrice")
    ticker = str(body.get("symbol", "")).upper()
    # SECURITY: multiplier MUST come from server-side universe metadata, never
    # from request body. A client posting `multiplier: 1` would otherwise
    # inflate share-cover units 100x and bypass Gate 4. Unknown tickers fall
    # back to 100 so the universe check can produce UNIVERSE_UNKNOWN rather
    # than a KeyError.
    multiplier = get_multiplier(ticker) if ticker in UNIVERSE else 100
    return PreflightRequest(
        ticker=ticker,
        security_type=sec_type,
        action=str(body.get("action", "")).upper(),
        quantity=int(body.get("quantity", 0)),
        right=right,
        expiry=body.get("expiry"),
        strike=Decimal(str(body["strike"])) if body.get("strike") is not None else None,
        multiplier=multiplier,
        limit_price=Decimal(str(limit)) if limit is not None else Decimal("0"),
    )


def _body_to_combo_preflight_request(body: dict) -> preflight.ComboPreflightRequest:
    from xenon.execution.universe import UNIVERSE, get_multiplier

    ticker = str(body.get("symbol", "")).upper()
    multiplier = get_multiplier(ticker) if ticker in UNIVERSE else 100
    legs = []
    for leg in body.get("legs") or []:
        right_raw = str(leg.get("right") or "").upper()
        if right_raw not in ("C", "P"):
            raise ValueError(f"combo leg right must be C or P, got {right_raw!r}")
        legs.append(
            preflight.ComboPreflightLeg(
                expiry=leg.get("expiry"),
                strike=Decimal(str(leg["strike"])) if leg.get("strike") is not None else None,
                right=right_raw,
                action=str(leg.get("action", "")).upper(),
                ratio=int(leg.get("ratio") or 1),
            )
        )
    return preflight.ComboPreflightRequest(
        ticker=ticker,
        action=str(body.get("action", "")).upper(),
        quantity=int(body.get("quantity", 0)),
        multiplier=multiplier,
        legs=legs,
    )


def _normalize_gate_expiry(expiry: Any) -> str | None:
    if not expiry:
        return None
    clean = str(expiry).replace("-", "")
    return clean if len(clean) == 8 and clean.isdigit() else None


def _portfolio_has_matching_long_option(
    portfolio: PortfolioView,
    *,
    ticker: str,
    expiry: str | None,
    strike: Decimal | None,
    right: str | None,
    quantity: int,
) -> bool:
    if strike is None or right not in {"C", "P"}:
        return False
    expected_type = "Call" if right == "C" else "Put"
    wanted_expiry = _normalize_gate_expiry(expiry)
    if wanted_expiry is None:
        return False
    held = 0
    for pos in portfolio.positions:
        if pos.ticker.upper() != ticker.upper():
            continue
        if _normalize_gate_expiry(pos.expiry) != wanted_expiry:
            continue
        for leg in pos.legs:
            if leg.direction == "LONG" and leg.type == expected_type and leg.strike == strike:
                held += int(leg.contracts)
    return held >= quantity


async def _is_regime_gate_risk_reducing_exit(body: dict) -> bool:
    """True when /orders/place is reducing an existing exposure.

    RegimeGate blocks new exposure. To bypass it, a SELL must be backed by
    portfolio evidence — an existing long option for single-leg SELL, or
    a per-leg inverse cover for combo SELL. Stock SELL bypasses (Gate 4
    naked-short audit catches the rest).

    Fail-closed on a stale or missing portfolio snapshot: a 30-min-old
    snapshot during a fast-moving panic can falsely "prove" a long still
    exists, so we refuse the bypass instead and let RegimeGate block.
    """
    action = str(body.get("action", "")).upper()
    if action != "SELL":
        return False
    if body.get("type") == "stock":
        return True

    portfolio, snapshot_at = _unpack_portfolio_load(await _load_portfolio_view())
    if portfolio is None:
        return False
    if _portfolio_snapshot_stale_response(snapshot_at) is not None:
        return False

    if body.get("type") == "combo":
        try:
            combo_req = _body_to_combo_preflight_request(body)
        except (ValueError, ValidationError):
            return False
        return preflight.combo_close_covered_by_portfolio(combo_req, portfolio)

    if body.get("type") != "option":
        return False
    try:
        req = _body_to_preflight_request(body)
    except (ValueError, ValidationError):
        return False
    return _portfolio_has_matching_long_option(
        portfolio,
        ticker=req.ticker,
        expiry=req.expiry,
        strike=req.strike,
        right=req.right,
        quantity=req.quantity,
    )


def _invalid_order_body_response(exc: Exception) -> Verdict:
    return Verdict(
        accept=False,
        reason_code=ReasonCode.INVALID_ORDER_BODY,
        reason_detail=str(exc),
    )


def _portfolio_required_response(body: dict) -> Verdict | None:
    order_type = body.get("type")
    action = str(body.get("action", "")).upper()
    if order_type == "combo":
        req = _body_to_combo_preflight_request(body)
        if preflight.combo_uncovered_short_call_ratio(req) > 0:
            return Verdict(
                accept=False,
                reason_code=ReasonCode.PORTFOLIO_SNAPSHOT_REQUIRED,
                reason_detail="Portfolio snapshot required to verify combo short-call coverage",
            )
        return None
    if action != "SELL":
        return None
    if order_type == "option" and str(body.get("right", "")).upper() == "P":
        return None
    return Verdict(
        accept=False,
        reason_code=ReasonCode.PORTFOLIO_SNAPSHOT_REQUIRED,
        reason_detail="Portfolio snapshot required to verify short exposure",
    )


async def _run_preflight(body: dict, user_id: str = "local", cover_ratio: float = 1.0) -> Verdict:
    """Run server-side Gate 4. `cover_ratio` is plumbed from RegimeGate
    when binding_tier is TIER_2 (1.25 → 125 shares per short call)."""
    if body.get("type") == "combo":
        try:
            req = _body_to_combo_preflight_request(body)
        except (ValueError, ValidationError) as exc:
            return _invalid_order_body_response(exc)
        if preflight.combo_uncovered_short_call_ratio(req) <= 0 or req.action == "SELL":
            return preflight.evaluate_combo(req, PortfolioView(), cover_ratio=cover_ratio)
        portfolio, snapshot_at = _unpack_portfolio_load(await _load_portfolio_view())
        if portfolio is None:
            try:
                missing = _portfolio_required_response(body)
            except (ValueError, ValidationError) as exc:
                return _invalid_order_body_response(exc)
            if missing is not None:
                return missing
            portfolio = PortfolioView()
        stale = _portfolio_snapshot_stale_response(snapshot_at)
        if stale is not None:
            return stale
        reservations = orders_store.working_reservations_for(user_id, req.ticker, **_resolve_scope_kwargs())
        return preflight.evaluate_combo(req, portfolio, reservations=reservations, cover_ratio=cover_ratio)

    try:
        req = _body_to_preflight_request(body)
    except (ValueError, ValidationError) as exc:
        return _invalid_order_body_response(exc)
    if req.action == "BUY":
        return preflight.evaluate(req, PortfolioView(), cover_ratio=cover_ratio)

    portfolio, snapshot_at = _unpack_portfolio_load(await _load_portfolio_view())
    if portfolio is None:
        missing = _portfolio_required_response(body)
        if missing is not None:
            return missing
        portfolio = PortfolioView()
    stale = _portfolio_snapshot_stale_response(snapshot_at)
    if stale is not None:
        return stale
    reservations = orders_store.working_reservations_for(user_id, req.ticker, **_resolve_scope_kwargs())
    return preflight.evaluate(req, portfolio, reservations=reservations, cover_ratio=cover_ratio)


def _lookup_min_tick_via_pool(con_id: int) -> Decimal:
    """Front-of-house minTick approximation. Returns 0.01 for every contract.

    **By design.** Replicating IB's tick-rule table locally is fragile:
    Reg NMS Rule 612 sub-penny carve-outs for stocks under $1, OPRA penny
    pilot rosters for options, and exchange-specific market rules for
    futures all change without notice. Instead of reaching into IB on the
    sync ``quote_guard.check`` path (which would require ``ib_async``'s
    ``reqContractDetailsAsync`` and a wider sync→async refactor), we keep
    the cheap 0.01 approximation **and rely on IB to reject off-tick
    prices server-side**.

    When IB rejects with error code 110 ("price does not conform to
    minimum price variation"), ``_orders_place_from_body`` re-maps the
    response from generic ``IB_REJECT`` to ``LIMIT_OFF_TICK`` so the UI
    surfaces a clean message and the IB code+message land in the
    ``orders_events`` audit row for later analysis.

    Most US equities and most options at the prices we trade have $0.01
    ticks, so this approximation covers the common case correctly. Edge
    instruments (sub-$1 stocks, foreign equities, futures, index option
    strike bands) will hit IB's rejection — that's acceptable: the
    user-visible error is actionable and we have a structured log to
    measure how often it happens.

    If you ever need real tick lookups (e.g. for fractional-share / sub-$1
    stock support), the right path is to plumb ``reqContractDetailsAsync``
    through an async ``quote_guard``, not to patch this stub.
    """
    return Decimal("0.01")


_tick_rule_cache = quote_guard.TickRuleCache(
    source=_lookup_min_tick_via_pool,
    ttl_seconds=24 * 3600,
)


def _now() -> datetime:
    """Test seam: override via monkeypatch to inject a fixed RTH timestamp."""
    return datetime.now(tz=timezone.utc)


def _ensure_thread_event_loop() -> None:
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _ticker_to_quote_snapshot(ticker: str, con_id: int, tk: Any) -> dict:
    import math as _math

    bid = getattr(tk, "bid", None)
    ask = getattr(tk, "ask", None)
    bid_size = getattr(tk, "bidSize", 0) or 0
    ask_size = getattr(tk, "askSize", 0) or 0

    if (
        bid is None
        or ask is None
        or (isinstance(bid, float) and _math.isnan(bid))
        or (isinstance(ask, float) and _math.isnan(ask))
    ):
        raise HTTPException(status_code=503, detail=f"No quote available for {ticker}/{con_id}")
    return {
        "bid": Decimal(str(bid)),
        "ask": Decimal(str(ask)),
        "bid_size": int(bid_size),
        "ask_size": int(ask_size),
    }


def _fetch_quote_snapshot_with_client(client: Any, ticker: str, con_id: int) -> dict:
    """Run blocking ib_async quote work in a worker thread that owns a loop."""
    _ensure_thread_event_loop()
    contract = Contract(conId=int(con_id), exchange="SMART")
    qualified = client.qualify_contract(contract)
    tk = client.get_quote(qualified, snapshot=True)
    return _ticker_to_quote_snapshot(ticker, con_id, tk)


def _contract_from_order_body(body: dict) -> Contract:
    symbol = str(body.get("symbol", "")).upper()
    if body.get("type") == "option":
        return Option(
            symbol=symbol,
            lastTradeDateOrContractMonth=str(body.get("expiry") or ""),
            strike=float(body.get("strike") or 0),
            right=str(body.get("right") or "").upper(),
            exchange="SMART",
            currency="USD",
        )
    return Stock(symbol, "SMART", "USD")


def _fetch_order_quote_snapshot_with_client(client: Any, body: dict) -> tuple[int, dict]:
    """Qualify a stock/option order body and fetch its snapshot quote."""
    _ensure_thread_event_loop()
    ticker = str(body.get("symbol", "")).upper()
    contract = _contract_from_order_body(body)
    qualified = client.qualify_contract(contract)
    con_id = int(getattr(qualified, "conId", 0) or 0)
    if con_id <= 0:
        raise HTTPException(status_code=503, detail=f"Could not qualify contract for {ticker}")
    tk = client.get_quote(qualified, snapshot=True)
    return con_id, _ticker_to_quote_snapshot(ticker, con_id, tk)


def _qualify_order_con_id_with_client(client: Any, body: dict) -> int:
    """Qualify a stock/option order body and return its IB conId."""
    _ensure_thread_event_loop()
    ticker = str(body.get("symbol", "")).upper()
    contract = _contract_from_order_body(body)
    qualified = client.qualify_contract(contract)
    con_id = int(getattr(qualified, "conId", 0) or 0)
    if con_id <= 0:
        raise HTTPException(status_code=503, detail=f"Could not qualify contract for {ticker}")
    return con_id


async def _fetch_quote_snapshot(ticker: str, con_id: int) -> dict:
    """Fetch a bid/ask snapshot from the serialized ib_pool 'data' role."""
    pool = ib_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="IB data role unavailable")

    try:
        async with pool.acquire("data") as client:
            return await pool.run_sync(
                "data",
                _fetch_quote_snapshot_with_client,
                client,
                ticker,
                con_id,
            )
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _fetch_order_quote_snapshot(body: dict) -> tuple[int, dict]:
    ticker = str(body.get("symbol", "")).upper()
    con_id = int(body.get("con_id") or 0)
    if con_id > 0:
        return con_id, await _fetch_quote_snapshot(ticker, con_id)

    pool = ib_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="IB data role unavailable")

    try:
        async with pool.acquire("data") as client:
            return await pool.run_sync(
                "data",
                _fetch_order_quote_snapshot_with_client,
                client,
                dict(body),
            )
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _qualify_order_con_id(body: dict) -> int:
    con_id = int(body.get("con_id") or 0)
    if con_id > 0:
        return con_id

    pool = ib_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="IB data role unavailable")

    try:
        async with pool.acquire("data") as client:
            return await pool.run_sync(
                "data",
                _qualify_order_con_id_with_client,
                client,
                dict(body),
            )
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _quote_payload_from_snapshot(ticker: str, con_id: int, snap: dict) -> QuotePayload:
    return QuotePayload(
        con_id=int(con_id),
        ticker=ticker.upper(),
        bid=Decimal(str(snap["bid"])),
        ask=Decimal(str(snap["ask"])),
        bid_size=int(snap["bid_size"]),
        ask_size=int(snap["ask_size"]),
        ts_server_ms=int(time.time() * 1000),
    )


async def _validate_non_combo_quote(body: dict) -> tuple[quote_guard.QuoteVerdict, int]:
    ticker = str(body.get("symbol", "")).upper()
    security_type = "STK" if body.get("type") == "stock" else "OPT"
    action = str(body.get("action", "")).upper()
    limit_price = Decimal(str(body.get("limitPrice", "0")))
    now = _now()

    market = quote_guard.check_market_hours(security_type=security_type, now=now)
    if not market.accept:
        return market, 400

    payload: QuotePayload | None = None
    token = body.get("quote_token")
    if token:
        try:
            payload = quote_tokens.verify(
                token,
                os.environ.get("XENON_QUOTE_TOKEN_SECRET", ""),
                max_age_ms=quote_guard.MAX_AGE_RTH_MS,
            )
        except quote_tokens.QuoteTokenExpired:
            payload = None
        except quote_tokens.QuoteTokenInvalid as exc:
            return (
                quote_guard.QuoteVerdict(
                    accept=False,
                    reason_code=ReasonCode.STALE_QUOTE,
                    reason_detail=f"token invalid: {exc}",
                ),
                400,
            )

    con_id = int(body.get("con_id") or 0)
    if payload is not None and body.get("type") == "option" and con_id <= 0:
        try:
            con_id = await _qualify_order_con_id(body)
        except HTTPException as exc:
            return (
                quote_guard.QuoteVerdict(
                    accept=False,
                    reason_code=ReasonCode.QUOTE_UNAVAILABLE,
                    reason_detail=str(exc.detail),
                ),
                exc.status_code if exc.status_code >= 500 else 400,
            )

    if payload is None:
        try:
            con_id, snap = await _fetch_order_quote_snapshot(body)
        except HTTPException as exc:
            return (
                quote_guard.QuoteVerdict(
                    accept=False,
                    reason_code=ReasonCode.QUOTE_UNAVAILABLE,
                    reason_detail=str(exc.detail),
                ),
                exc.status_code if exc.status_code >= 500 else 400,
            )
        payload = _quote_payload_from_snapshot(ticker, con_id, snap)

    expected_con_id = con_id or payload.con_id
    return (
        quote_guard.check_payload(
            payload=payload,
            con_id=expected_con_id,
            ticker=ticker,
            security_type=security_type,
            action=action,
            limit_price=limit_price,
            now=now,
            tick_rule_lookup=_tick_rule_cache.get,
        ),
        400,
    )


@app.get("/orders/quote")
async def orders_quote(ticker: str, con_id: int):
    secret = os.environ.get("XENON_QUOTE_TOKEN_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="quote secret not configured")
    snap = await _fetch_quote_snapshot(ticker, con_id)
    import time as _time

    payload = QuotePayload(
        con_id=con_id,
        ticker=ticker.upper(),
        bid=snap["bid"],
        ask=snap["ask"],
        bid_size=snap["bid_size"],
        ask_size=snap["ask_size"],
        ts_server_ms=int(_time.time() * 1000),
    )
    token = quote_tokens.mint(payload, secret)
    return {
        "token": token,
        "bid": str(payload.bid),
        "ask": str(payload.ask),
        "bid_size": payload.bid_size,
        "ask_size": payload.ask_size,
        "ts_server_ms": payload.ts_server_ms,
    }


@app.post("/orders/place")
async def orders_place(request: Request):
    """Place an order via IB (on-demand connection, client_id=26)."""
    if is_read_only():
        return read_only_403()
    broker = str(getattr(request.app.state, "broker", "IB") or "IB").upper()
    if broker == "IB":
        require_mode_verified(request)
    body = await request.json()
    return await _orders_place_from_body(body)


def _resolve_scope_kwargs() -> dict[str, str]:
    """Resolve broker account scope from app.state with safe fallback.

    Lifespan populates `app.state.trading_mode` and `app.state.account`.
    Tests that bypass lifespan get `legacy_unknown` defaults — this matches
    the column server_default so existing test rows still pass CHECK.
    """
    try:
        return resolve_from_app_state(app.state).as_dict()
    except ValueError:
        pass
    return {"broker": "IB", "account_env": "legacy_unknown", "broker_account": "legacy_unknown"}


_REGIME_OVERRIDE_MIN_REASON_CHARS = 10


def _resolve_scope_obj():
    """Return AccountScope for in-process callers. Falls back when lifespan didn't run."""
    from xenon.execution.account_scope import AccountScope

    try:
        return resolve_from_app_state(app.state)
    except ValueError:
        return AccountScope(broker="IB", account_env="legacy_unknown", broker_account="legacy_unknown")


async def _resolve_regime_bankroll_usd(scope) -> float:
    """Bankroll input for RegimeGate, sourced from real account NAV.

    Precedence: env override (test/dev) → latest `account_snapshots.net_liquidation`
    for this scope → small conservative fail-safe. The fail-safe is intentionally
    tight — at TIER_2, a small bankroll yields a small max-loss cap, so an
    unknown-NAV order is more likely to require the user to resize down rather
    than slip through with a $100k assumption.
    """
    raw = os.environ.get("XENON_REGIME_BANKROLL_USD_OVERRIDE")
    if raw is not None and raw.strip():
        try:
            return float(raw)
        except ValueError:
            pass
    try:
        from xenon.db.engine import get_engine
        from xenon.db.queries.portfolio import get_latest_net_liquidation_for_scope

        engine = get_engine()
        async with engine.connect() as conn:
            net_liq = await get_latest_net_liquidation_for_scope(
                conn,
                broker=scope.broker,
                account_env=scope.account_env,
                broker_account=scope.broker_account,
            )
        if net_liq is not None and net_liq > 0:
            return float(net_liq)
    except Exception as exc:
        logger.warning("[regime] Could not load net_liquidation for %s: %s", scope.as_dict(), exc)
    return resolve_bankroll_usd()


async def _run_regime_gate(body: dict):
    """Evaluate the regime gate against an order body.

    Returns `(outcome_or_None, block_response_or_None)`:
    - When the gate would BLOCK without a valid override → outcome=None,
      block_response is a JSONResponse (409) the caller returns.
    - When the gate would 422 (THROTTLE + max_loss > cap) → same shape
      with status 422.
    - When the gate decides OK or THROTTLE within cap, or BLOCK with a
      valid override, → outcome is set, block_response is None and the
      caller continues with cover_ratio + override_audit.
    - When gating is disabled (test mode default), outcome is None and
      block_response is None — caller proceeds with cover_ratio=1.0.
    """
    if os.environ.get("XENON_REGIME_GATE_DISABLED") == "1" or (
        _is_test_mode() and os.environ.get("XENON_REGIME_GATE_IN_TESTS") != "1"
    ):
        return None, None

    if await _is_regime_gate_risk_reducing_exit(body):
        return None, None

    try:
        if body.get("type") == "combo":
            gate_req = _body_to_combo_preflight_request(body)
            net_price = Decimal(str(body["limitPrice"])) if body.get("limitPrice") is not None else None
        else:
            gate_req = _body_to_preflight_request(body)
            net_price = None
    except (ValueError, ValidationError) as exc:
        return None, JSONResponse(
            status_code=400,
            content={
                "detail": str(exc),
                "reason_code": ReasonCode.INVALID_ORDER_BODY.value,
                "reason_detail": str(exc),
            },
        )

    scope = _resolve_scope_obj()
    state = await get_regime_state_for_scope(scope)
    bankroll_usd = await _resolve_regime_bankroll_usd(scope)
    outcome = evaluate_order_gate(gate_req, state, bankroll_usd=bankroll_usd, net_price=net_price)

    if outcome.decision is GateDecision.BLOCK:
        override_requested = bool(body.get("override"))
        override_reason = str(body.get("override_reason") or "").strip()
        if not override_requested or len(override_reason) < _REGIME_OVERRIDE_MIN_REASON_CHARS:
            return None, JSONResponse(
                status_code=409,
                content={
                    "detail": outcome.reason,
                    "reason_code": "REGIME_BLOCK",
                    "decision": "block",
                    "binding_tier": state.binding_tier,
                    "binding_side": outcome.bind,
                    "vcg_tier": state.vcg_tier,
                    "cri_tier": state.cri_tier,
                    "override_required": True,
                    "override_min_reason_chars": _REGIME_OVERRIDE_MIN_REASON_CHARS,
                },
            )
        return outcome, None

    if outcome.exceeds_throttle_cap:
        max_loss_payload = outcome.max_loss_usd if outcome.max_loss_usd != math.inf else None
        return None, JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"{state.binding_tier} throttle: order's max loss exceeds the "
                    f"per-order cap (${outcome.max_loss_cap_usd:.0f})"
                ),
                "reason_code": "REGIME_RESIZE_REQUIRED",
                "decision": "resize_required",
                "binding_tier": state.binding_tier,
                "binding_side": outcome.bind,
                "max_loss_usd": max_loss_payload,
                "max_loss_cap_usd": outcome.max_loss_cap_usd,
                "cover_ratio": outcome.cover_ratio,
            },
        )

    return outcome, None


def _build_override_audit(body: dict, outcome) -> dict | None:
    """Pack override data for orders_store.reserve_attempt.

    Returns None unless the order is proceeding via override (i.e. the
    gate would have BLOCK'd but body['override'] + override_reason were
    valid). Caller already validated the reason length in _run_regime_gate.
    """
    if outcome is None or outcome.decision is not GateDecision.BLOCK:
        return None
    state = outcome.state
    return {
        "route": "POST /orders/place",
        "vcg_tier": state.vcg_tier,
        "cri_tier": state.cri_tier,
        "binding_side": outcome.bind,
        "block_reason": outcome.reason,
        "user_reason": str(body.get("override_reason") or "").strip(),
        "order_payload": body,
    }


async def _orders_place_from_body(body: dict):
    broker = str(getattr(app.state, "broker", "IB") or "IB").upper()
    if broker != "IB":
        return JSONResponse(
            status_code=403,
            content={
                "detail": f"{broker} is read-only for order placement",
                "reason_code": ReasonCode.READ_ONLY_BROKER.value,
                "reason_detail": f"{broker} is read-only for order placement",
            },
        )

    scope = _resolve_scope_kwargs()
    if scope["broker"] != "IB":
        return JSONResponse(
            status_code=403,
            content={
                "detail": f"{scope['broker']} is read-only for order placement",
                "reason_code": ReasonCode.READ_ONLY_BROKER.value,
                "reason_detail": f"{scope['broker']} is read-only for order placement",
            },
        )

    # Phase 3 — RegimeGate. Runs before preflight so its cover_ratio
    # (1.25 on TIER_2) tightens Gate 4 in the same pass. Tests bypass
    # via XENON_REGIME_GATE_DISABLED=1 unless they want to exercise it.
    gate_outcome, gate_block_response = await _run_regime_gate(body)
    if gate_block_response is not None:
        return gate_block_response
    cover_ratio_for_preflight = gate_outcome.cover_ratio if gate_outcome else 1.0
    override_audit = _build_override_audit(body, gate_outcome) if gate_outcome else None

    # F2: server-side Gate 4. Run preflight before any subprocess invocation.
    verdict = await _run_preflight(body, cover_ratio=cover_ratio_for_preflight)
    if not verdict.accept:
        code = verdict.reason_code.value if verdict.reason_code else None
        # `detail` is the field web/lib/xenonApi.ts:39 reads for human-
        # readable error copy; include the reason_code fields too so F6
        # can drive structured UI toast mapping.
        return JSONResponse(
            status_code=400,
            content={
                "detail": verdict.reason_detail or code or "Preflight blocked",
                "reason_code": code,
                "reason_detail": verdict.reason_detail,
            },
        )

    # F3: quote-token + tick-grid + limit-band + market-hours
    if body.get("type") != "combo":
        qv, quote_status = await _validate_non_combo_quote(body)
        _override_detail = None
        if not qv.accept:
            if qv.reason_code == ReasonCode.LIMIT_OUT_OF_BAND and body.get("acknowledge_limit_override") is True:
                _override_detail = {
                    "reason_detail": qv.reason_detail,
                    "limit_price": body.get("limitPrice"),
                }
            else:
                return JSONResponse(
                    status_code=quote_status,
                    content={
                        "detail": qv.reason_detail,
                        "reason_code": qv.reason_code.value if qv.reason_code else None,
                        "reason_detail": qv.reason_detail,
                    },
                )
    else:
        _override_detail = None

    # F4: atomic reservation
    cid = body.get("client_attempt_id")
    if not cid:
        return JSONResponse(
            status_code=400,
            content={"detail": "client_attempt_id is required"},
        )
    user_id = "local"
    security_type = "STK" if body.get("type") == "stock" else "BAG" if body.get("type") == "combo" else "OPT"
    req_row = orders_store.RequestRow(
        ticker=str(body.get("symbol", "")).upper(),
        security_type=security_type,
        action=str(body.get("action", "")).upper(),
        quantity=int(body.get("quantity", 0)),
        expiry=body.get("expiry"),
        strike=Decimal(str(body["strike"])) if body.get("strike") is not None else None,
        right=(body.get("right") or "").upper() or None,
        multiplier=int(body.get("multiplier", 100)),
        con_id=int(body.get("con_id") or 0) or None,
        limit_price=Decimal(str(body.get("limitPrice", "0"))),
    )
    outcome = orders_store.reserve_attempt(
        user_id,
        cid,
        req_row,
        override_audit=override_audit,
        **_resolve_scope_kwargs(),
    )
    if outcome.status == "terminal":
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"attempt {cid} already in terminal state {outcome.state}",
                "reason_code": ReasonCode.ATTEMPT_ID_TERMINAL.value,
                "state": outcome.state,
                "prior_reason_code": outcome.reason_code,
            },
        )
    if outcome.status == "duplicate":
        return JSONResponse(
            status_code=200,
            content={
                "duplicate_of": outcome.duplicate_of,
                "state": outcome.state,
                "submission_id": outcome.submission_id,
            },
        )
    submission_id = outcome.submission_id

    if _override_detail is not None:
        orders_store.record_event(submission_id, "PREFLIGHT_ACK_LIMIT", _override_detail)

    if _is_test_mode():
        order_id, perm_id = _next_test_order_ids()
        orders_store.mark_submitted(
            submission_id=submission_id,
            ib_order_id=str(order_id),
            perm_id=str(perm_id),
            placing_client_id=26,
        )
        return {
            "status": "ok",
            "orderId": order_id,
            "permId": perm_id,
            "initialStatus": "Submitted",
            "message": "Order accepted in test mode",
            "echo": body,
            "submission_id": submission_id,
        }

    order_json = json.dumps(body)
    result = await _run_ib_script_with_recovery("xenon-ib-place-order", ["--json", order_json], timeout=15)
    if not result.ok:
        orders_store.mark_terminal(
            submission_id=submission_id,
            state="FAILED",
            reason_code="SUBPROCESS_ERROR",
            filled_qty=0,
            avg_fill_price=None,
        )
        raise HTTPException(status_code=502, detail=result.error)
    if result.data and result.data.get("status") == "error":
        # B6 — write a reason_code the UI toast map can resolve. Most IB
        # rejections collapse to IB_REJECT, but tick-rule rejections
        # (code 110) re-map to LIMIT_OFF_TICK so the user sees a clean
        # actionable message instead of raw IB error text. The raw IB
        # numeric code + message always go into the orders_events audit
        # row so we don't lose the info.
        ib_code = result.data.get("code")
        ib_message = result.data.get("message", "Order failed")
        if str(ib_code) == "110":
            reason_code = ReasonCode.LIMIT_OFF_TICK.value
            logger.warning(
                "IB rejected order with tick-rule violation: submission=%s ib_code=110 ib_message=%s",
                submission_id,
                ib_message,
            )
        else:
            reason_code = ReasonCode.IB_REJECT.value
        orders_store.mark_terminal(
            submission_id=submission_id,
            state="REJECTED",
            reason_code=reason_code,
            filled_qty=0,
            avg_fill_price=None,
        )
        try:
            orders_store.record_event(
                submission_id,
                "IB_REJECT",
                {"ib_code": ib_code, "ib_message": ib_message},
            )
        except Exception:  # pragma: no cover — event writes are best-effort
            logger.warning(
                "Failed to record IB_REJECT event for submission %s",
                submission_id,
                exc_info=True,
            )
        raise HTTPException(status_code=502, detail=ib_message)
    if result.data:
        orders_store.mark_submitted(
            submission_id=submission_id,
            ib_order_id=str(result.data.get("orderId") or ""),
            perm_id=str(result.data.get("permId") or ""),
            placing_client_id=int(result.data.get("clientId") or 26),
        )
    return result.data


# F5.4 — cancel/modify failure classification
# ib_reject codes that mean "order vanished" map to 404 so the UI can
# distinguish from a hard reject. All other ib_reject codes are 400.
_IB_REJECT_NOT_FOUND_CODES = {"10147", "10148"}


def _map_ib_reject_status(upstream_code: Any) -> int:
    code = str(upstream_code) if upstream_code is not None else ""
    return 404 if code in _IB_REJECT_NOT_FOUND_CODES else 400


def _classify_to_http(data: dict) -> tuple[int, str]:
    """Map subprocess classification → (http_status, reason_code)."""
    classification = data.get("classification")
    if classification == "connection":
        return 503, ReasonCode.IB_CONNECTION.value
    if classification == "ownership":
        return 409, ReasonCode.OWNERSHIP.value
    if classification == "ib_reject":
        upstream = data.get("upstream") or {}
        return _map_ib_reject_status(upstream.get("code")), ReasonCode.IB_REJECT.value
    # Unknown/missing classification — fall back to 502 with a generic code.
    return 502, ReasonCode.IB_REJECT.value


def _record_manage_event(
    ib_order_id: str,
    kind: str,
    detail: dict,
    perm_id: str = "",
    scope: dict[str, str] | None = None,
) -> None:
    """Write orders_events row for a cancel/modify attempt.

    Resolves the submission by ib_order_id when present, else falls back to
    perm_id (UI-initiated cancel/modify often carries ``orderId=0`` and only
    a permId). If no submission exists (order placed pre-F4, or before a
    reserve_attempt row was created), the event is skipped — orders_events
    .submission_id is NOT NULL and has no synthetic parent row available.
    """
    try:
        scope = scope or _resolve_scope_kwargs()
        sid: str | None = None
        if ib_order_id:
            sid = orders_store.lookup_submission_id_by_ib_order_id(ib_order_id, **scope)
        if sid is None and perm_id:
            sid = orders_store.lookup_submission_id_by_perm_id(perm_id, **scope)
        if sid:
            orders_store.record_event(sid, kind, detail)
    except Exception:  # pragma: no cover — event writes are best-effort
        logger.warning(
            "Failed to record %s event for order=%s perm=%s",
            kind,
            ib_order_id,
            perm_id,
            exc_info=True,
        )


@app.post("/orders/cancel", dependencies=[Depends(require_mode_verified)])
async def orders_cancel(request: Request):
    """Cancel an open order via subprocess.

    IB scopes cancelOrder by clientId — only the clientId that placed the
    order can cancel it. The subprocess detects the original clientId and
    reconnects as that client before cancelling.

    F5.4 classifies subprocess failures into HTTP statuses:
      classification=connection → 503 IB_CONNECTION
      classification=ownership  → 409 OWNERSHIP
      classification=ib_reject  → 400 (or 404 for 10147/10148) IB_REJECT
    The full upstream payload (code + message) is preserved in detail.
    """
    if is_read_only():
        return read_only_403()
    body = await request.json()
    return await _orders_cancel_from_body(body)


async def _orders_cancel_from_body(body: dict):
    if _is_test_mode():
        return {
            "status": "ok",
            "message": "Cancel accepted in test mode",
            "echo": body,
        }

    order_id = body.get("orderId", 0)
    perm_id = body.get("permId", 0)

    args = ["cancel"]
    if order_id:
        args.extend(["--order-id", str(order_id)])
    if perm_id:
        args.extend(["--perm-id", str(perm_id)])

    result = await _run_ib_script_with_recovery("xenon-ib-order-manage", args, timeout=15)
    if not result.ok:
        detail = {
            "reason_code": ReasonCode.IB_CONNECTION.value,
            "message": result.error or "Subprocess failed",
            "http_status": 503,
        }
        _record_manage_event(str(order_id or ""), "CANCEL", detail, perm_id=str(perm_id or ""))
        raise HTTPException(status_code=503, detail=detail)

    data = result.data or {}
    if data.get("status") == "error":
        http_status, reason_code = _classify_to_http(data)
        detail = {
            "reason_code": reason_code,
            "classification": data.get("classification"),
            "message": data.get("message"),
            "upstream": data.get("upstream"),
            "http_status": http_status,
        }
        _record_manage_event(str(order_id or ""), "CANCEL", detail, perm_id=str(perm_id or ""))
        raise HTTPException(status_code=http_status, detail=detail)

    _record_manage_event(
        str(order_id or ""),
        "CANCEL",
        {"status": data.get("status"), "message": data.get("message"), "http_status": 200},
        perm_id=str(perm_id or ""),
    )
    _mark_submission_cancelled(str(order_id or ""), str(perm_id or ""))
    return data


def _mark_submission_cancelled(ib_order_id: str, perm_id: str) -> None:
    """Transition the order_submissions row to CANCELLED after a successful
    /orders/cancel. The activity poller cannot do this on its own — naive
    disappearance-detection misclassifies fills as cancels — so the cancel
    route is the only authoritative trigger.
    """
    try:
        scope = _resolve_scope_kwargs()
        sid: str | None = None
        if ib_order_id:
            sid = orders_store.lookup_submission_id_by_ib_order_id(ib_order_id, **scope)
        if sid is None and perm_id:
            sid = orders_store.lookup_submission_id_by_perm_id(perm_id, **scope)
        if sid is None:
            return
        orders_store.mark_terminal(
            submission_id=sid,
            state="CANCELLED",
            reason_code="USER_CANCEL",
            filled_qty=0,
            avg_fill_price=None,
        )
    except Exception:  # pragma: no cover — best-effort
        logger.warning(
            "Failed to mark submission CANCELLED for order=%s perm=%s",
            ib_order_id,
            perm_id,
            exc_info=True,
        )


@app.post("/orders/modify", dependencies=[Depends(require_mode_verified)])
async def orders_modify(request: Request):
    """Modify an open order via subprocess.

    Modify requires the original clientId that placed the order (IB scopes
    placeOrder by clientId). The subprocess detects the original clientId
    and reconnects as that client before modifying.

    F5.4: modifySequence monotonic gate runs BEFORE the subprocess.
      - missing modifySequence → 400 MODIFY_SEQUENCE_REQUIRED
      - unknown ib_order_id     → 404 ORDER_NOT_FOUND
      - stale sequence          → 409 MODIFY_STALE (detail.applied=<current>)
    Subprocess failures classified the same as cancel.
    """
    if is_read_only():
        return read_only_403()
    body = await request.json()
    return await _orders_modify_from_body(body)


async def _run_modify_regime_gate(
    *,
    submission: dict,
    new_quantity: int | None,
    new_price: Any = None,
):
    """Apply RegimeGate to a /orders/modify body per spec §4.6.1.

    Modify rules:
    - Pure price (newQuantity is None or == old): skip gate.
    - Quantity decrease: skip gate (reducing risk).
    - Quantity increase: gate the synthetic delta order. BLOCK → 409,
      THROTTLE over cap → 422. Combos are not gated yet (BAG modify
      with quantity change is rare; tracked as follow-up).
    - Side change: not supported by IB modify, would need a new place.

    Returns `(outcome_or_None, response_or_None)`. When both are None,
    the modify proceeds without gating.
    """
    if os.environ.get("XENON_REGIME_GATE_DISABLED") == "1" or (
        _is_test_mode() and os.environ.get("XENON_REGIME_GATE_IN_TESTS") != "1"
    ):
        return None, None

    if new_quantity is None:
        return None, None  # pure price modify
    try:
        new_qty = int(new_quantity)
    except (TypeError, ValueError):
        return None, None  # malformed → bubble through normal validation
    old_qty = int(submission.get("quantity") or 0)
    if new_qty <= old_qty:
        return None, None  # quantity decrease / unchanged

    delta_qty = new_qty - old_qty
    sec_type = submission.get("security_type")

    scope = _resolve_scope_obj()
    state = await get_regime_state_for_scope(scope)
    bankroll_usd = await _resolve_regime_bankroll_usd(scope)

    if sec_type == "BAG":
        # order_submissions does not persist combo legs, so we can't build
        # a synthetic delta order to evaluate. At NORMAL there is no gate
        # anyway. At any restrictive tier, the conservative behavior is to
        # refuse the modify and require the user to cancel + replace through
        # the gated /orders/place path (which reconstructs legs from the
        # request body). Without this, a BAG qty-increase silently bypasses
        # the gate during PANIC.
        if state.binding_tier == "NORMAL":
            return None, None
        logger.info(
            "regime_gate(modify): BAG quantity-increase blocked at tier=%s (submission=%s)",
            state.binding_tier,
            submission.get("submission_id"),
        )
        return None, JSONResponse(
            status_code=409,
            content={
                "detail": (
                    f"{state.binding_tier} — combo quantity-increase modifies are "
                    "not supported under regime gating; cancel and replace through /orders/place"
                ),
                "reason_code": "REGIME_BLOCK",
                "decision": "block",
                "binding_tier": state.binding_tier,
                "binding_side": "vcg" if state.vcg_tier == state.binding_tier else "cri",
                "vcg_tier": state.vcg_tier,
                "cri_tier": state.cri_tier,
                "delta_quantity": delta_qty,
                "modify_path": True,
                "modify_sec_type": "BAG",
                "applied_sequence": int(submission.get("modify_sequence") or 0),
            },
        )

    try:
        limit_price = (
            Decimal(str(new_price)) if new_price is not None else submission.get("limit_price") or Decimal("0")
        )
        delta_req = PreflightRequest(
            ticker=submission["ticker"],
            security_type=sec_type or "OPT",
            action=submission["action"],
            quantity=delta_qty,
            right=submission.get("right"),
            expiry=submission.get("expiry").isoformat() if submission.get("expiry") else None,
            strike=submission.get("strike"),
            multiplier=int(submission.get("multiplier") or 100),
            limit_price=limit_price,
        )
    except (ValueError, ValidationError) as exc:
        return None, JSONResponse(
            status_code=400,
            content={
                "detail": str(exc),
                "reason_code": ReasonCode.INVALID_ORDER_BODY.value,
            },
        )

    outcome = evaluate_order_gate(delta_req, state, bankroll_usd=bankroll_usd)

    applied_sequence = int(submission.get("modify_sequence") or 0)
    if outcome.decision is GateDecision.BLOCK:
        return None, JSONResponse(
            status_code=409,
            content={
                "detail": outcome.reason,
                "reason_code": "REGIME_BLOCK",
                "decision": "block",
                "binding_tier": state.binding_tier,
                "binding_side": outcome.bind,
                "vcg_tier": state.vcg_tier,
                "cri_tier": state.cri_tier,
                "delta_quantity": delta_qty,
                "modify_path": True,
                "applied_sequence": applied_sequence,
                "override_required": True,
                "override_min_reason_chars": _REGIME_OVERRIDE_MIN_REASON_CHARS,
                "override_supported": False,
            },
        )
    if outcome.exceeds_throttle_cap:
        max_loss_payload = outcome.max_loss_usd if outcome.max_loss_usd != math.inf else None
        return None, JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"{state.binding_tier} throttle: delta order exceeds "
                    f"per-order cap (${outcome.max_loss_cap_usd:.0f})"
                ),
                "reason_code": "REGIME_RESIZE_REQUIRED",
                "decision": "resize_required",
                "binding_tier": state.binding_tier,
                "binding_side": outcome.bind,
                "max_loss_usd": max_loss_payload,
                "max_loss_cap_usd": outcome.max_loss_cap_usd,
                "delta_quantity": delta_qty,
                "modify_path": True,
                "applied_sequence": applied_sequence,
            },
        )
    return outcome, None


async def _orders_modify_from_body(body: dict):
    if _is_test_mode():
        return {
            "status": "ok",
            "message": "Modify accepted in test mode",
            "echo": body,
        }

    order_id = body.get("orderId", 0)
    perm_id = body.get("permId", 0)
    new_price = body.get("newPrice")
    new_quantity = body.get("newQuantity")
    outside_rth = body.get("outsideRth")
    modify_sequence = body.get("modifySequence")

    if modify_sequence is None:
        raise HTTPException(
            status_code=400,
            detail={
                "reason_code": ReasonCode.MODIFY_SEQUENCE_REQUIRED.value,
                "message": "modifySequence is required",
                "http_status": 400,
            },
        )
    try:
        modify_sequence = int(modify_sequence)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400,
            detail={
                "reason_code": ReasonCode.MODIFY_SEQUENCE_REQUIRED.value,
                "message": "modifySequence must be an integer",
                "http_status": 400,
            },
        )

    # Apply sequence gate BEFORE spawning the subprocess. If only permId is
    # supplied (UI-initiated modifies often have orderId=0), route through the
    # perm_id variant so we can still find the submissions row.
    if not order_id and not perm_id:
        raise HTTPException(
            status_code=400,
            detail={
                "reason_code": ReasonCode.ORDER_IDENTIFIER_REQUIRED.value,
                "message": "Modify request must include orderId or permId.",
                "http_status": 400,
            },
        )
    _scope = _resolve_scope_kwargs()
    submission = orders_store.load_submission_for_modify(
        order_id=str(order_id) if order_id else "",
        perm_id=str(perm_id) if perm_id else "",
        **_scope,
    )
    if submission is not None:
        _gate_outcome, gate_response = await _run_modify_regime_gate(
            submission=submission,
            new_quantity=new_quantity,
            new_price=new_price,
        )
        if gate_response is not None:
            return gate_response

    if not order_id and perm_id:
        seq_outcome = orders_store.apply_modify_by_perm_id(str(perm_id), modify_sequence, **_scope)
    else:
        seq_outcome = orders_store.apply_modify(str(order_id), modify_sequence, **_scope)
    if not seq_outcome["applied"]:
        current = seq_outcome["current_sequence"]
        if current == -1:
            raise HTTPException(
                status_code=404,
                detail={
                    "reason_code": ReasonCode.ORDER_NOT_FOUND.value,
                    "message": f"Order {order_id} not found",
                    "http_status": 404,
                },
            )
        raise HTTPException(
            status_code=409,
            detail={
                "reason_code": ReasonCode.MODIFY_STALE.value,
                "message": f"modifySequence {modify_sequence} is stale; current is {current}",
                "applied": current,
                "http_status": 409,
            },
        )

    args = ["modify"]
    if order_id:
        args.extend(["--order-id", str(order_id)])
    if perm_id:
        args.extend(["--perm-id", str(perm_id)])
    if new_price is not None:
        args.extend(["--new-price", str(new_price)])
    if new_quantity is not None:
        args.extend(["--new-quantity", str(new_quantity)])
    if outside_rth is True:
        args.append("--outside-rth")
    elif outside_rth is False:
        args.append("--no-outside-rth")

    result = await _run_ib_script_with_recovery("xenon-ib-order-manage", args, timeout=15)
    if not result.ok:
        # DB sequence is already advanced; don't roll back — prevents
        # double-apply on a retry. Log and surface to caller.
        logger.warning(
            "Modify subprocess failed after apply_modify(order=%s, seq=%s): %s",
            order_id,
            modify_sequence,
            result.error,
        )
        detail = {
            "reason_code": ReasonCode.IB_CONNECTION.value,
            "message": result.error or "Subprocess failed",
            "applied_sequence": modify_sequence,
            "http_status": 503,
        }
        _record_manage_event(str(order_id or ""), "MODIFY", detail, perm_id=str(perm_id or ""), scope=_scope)
        raise HTTPException(status_code=503, detail=detail)

    data = result.data or {}
    if data.get("status") == "error":
        http_status, reason_code = _classify_to_http(data)
        detail = {
            "reason_code": reason_code,
            "classification": data.get("classification"),
            "message": data.get("message"),
            "upstream": data.get("upstream"),
            "applied_sequence": modify_sequence,
            "http_status": http_status,
        }
        _record_manage_event(str(order_id or ""), "MODIFY", detail, perm_id=str(perm_id or ""), scope=_scope)
        raise HTTPException(status_code=http_status, detail=detail)

    _record_manage_event(
        str(order_id or ""),
        "MODIFY",
        {
            "status": data.get("status"),
            "message": data.get("message"),
            "applied_sequence": modify_sequence,
            "http_status": 200,
        },
        perm_id=str(perm_id or ""),
        scope=_scope,
    )
    # Echo the applied sequence so the UI can anchor its per-order counter.
    return {**data, "applied_sequence": modify_sequence}


# ---------------------------------------------------------------------------
# Phase 4: Market data & long-running endpoints (subprocess-based)
# ---------------------------------------------------------------------------


def _is_market_open_now() -> bool:
    """Live ET market-hours check — used to override stamped market_open at read time."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    et = _dt.now(tz=ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return False
    minutes = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


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
    new_warnings = new_result.get("warnings") or []
    new_count = new_result.get("count", 0)
    if not new_warnings:
        return None

    cached_path = DATA_DIR / "futu_portfolio.json"
    prev = _read_cache(cached_path)
    if prev is None:
        return None

    prev_count = prev.get("count", 0)
    if new_count >= prev_count:
        return None  # new snapshot isn't degraded

    error_path = DATA_DIR / "futu_portfolio.error.json"
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


@app.post("/futu/sync")
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
    - **Off-loop blocking calls.** The Futu SDK is fully sync; all calls
      run in the default thread pool executor.
    """
    global _futu_last_sync_monotonic
    import time as _time

    lock = _get_futu_lock()

    # Early cooldown check — avoids lock contention when cache is fresh.
    if _futu_in_cooldown():
        cached = _read_cache(DATA_DIR / "futu_portfolio.json")
        if cached:
            return {"ok": True, **cached}

    async with lock:
        # Re-check inside lock — race guard so two tabs inside the lock
        # window piggyback on the same result.
        if _futu_in_cooldown():
            cached = _read_cache(DATA_DIR / "futu_portfolio.json")
            if cached:
                return {"ok": True, **cached}

        client = _get_futu_client()
        loop = asyncio.get_running_loop()
        try:
            if not client.is_connected():
                await loop.run_in_executor(None, client.connect)
            result = await loop.run_in_executor(None, lambda: client.fetch_portfolio(force=True))
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

        _atomic_save(str(DATA_DIR / "futu_portfolio.json"), result)
        _futu_last_sync_monotonic = _time.monotonic()

        # Persist FUTU NAV to xenon.nav_history (spec §10). Best-effort —
        # a persistence failure must NOT mask a successful OpenD fetch.
        # NavAccountEnvConflict bubbles to a 409 so the operator sees it.
        engine = getattr(app.state, "db_engine", None)
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


@app.get("/futu/portfolio")
async def futu_portfolio():
    """Read the cached Futu portfolio snapshot.

    Does not hit OpenD — serves the last written `data/futu_portfolio.json`.
    Returns HTTP 200 with `{ok:false, code:"never_synced"}` on first boot
    so the UI can render a "click sync" prompt distinctly from errors.
    Clients that need fresh data call `POST /futu/sync`.
    """
    cached = _read_cache(DATA_DIR / "futu_portfolio.json")
    if cached is None:
        return dict(_FUTU_NEVER_SYNCED)
    return {"ok": True, **cached}


@app.post("/blotter")
async def blotter_sync(scope=Depends(get_account_scope)):
    """Return historical trades from Postgres first, then optional IB Flex.

    When IB_FLEX_TOKEN / IB_FLEX_QUERY_ID are unset, return a structured
    empty payload with configured=False rather than a 502, so the UI can
    show a friendly empty state. The Flex CLI emits exit code 2 + a
    JSON marker `{"error":"FLEX_NOT_CONFIGURED",...}` for that case
    (see src/xenon/trade_blotter/flex_query.py). Plan: docs/plans/
    2026-04-28-postgres-migration-completion-IMPL.md § W2.1.
    """
    engine = get_sync_engine()
    with engine.connect() as conn:
        pg_payload = fetch_blotter_pg(conn, scope=scope, days=30)

    pg_has = blotter_has_trades(pg_payload)
    result = await run_module("xenon.trade_blotter.flex_query", ["--json"], timeout=120)

    if not result.ok:
        is_unconfigured = result.exit_code == 2 or (result.error and "FLEX_NOT_CONFIGURED" in result.error)
        if is_unconfigured:
            if pg_has:
                return pg_payload
            return {
                "configured": False,
                "as_of": None,
                "summary": {
                    "closed_trades": 0,
                    "open_trades": 0,
                    "total_commissions": 0,
                    "realized_pnl": 0,
                },
                "closed_trades": [],
                "open_trades": [],
                "source": "none",
                "message": (
                    "IB Flex Query not configured. Set IB_FLEX_TOKEN and "
                    "IB_FLEX_QUERY_ID in .env, then click Refresh. Run "
                    "`uv run python -m xenon.trade_blotter.flex_query --setup` "
                    "for the configuration guide."
                ),
            }
        if pg_has:
            pg_payload["configured"] = True
            pg_payload["flex_error"] = result.error
            return pg_payload
        return {
            "configured": True,
            "flex_error": result.error,
            "as_of": None,
            "summary": {
                "closed_trades": 0,
                "open_trades": 0,
                "total_commissions": 0,
                "realized_pnl": 0,
            },
            "closed_trades": [],
            "open_trades": [],
            "source": "none",
            "message": (
                "IB Flex Query is configured but the fetch failed. "
                "If the error mentions code 1001, set the saved Flex query's "
                "format to XML in the IB portal (the legacy servlet rejects CSV)."
            ),
        }

    flex_payload = {**result.data, "configured": True}
    merged = merge_pg_and_flex(pg_payload, flex_payload)
    merged["configured"] = True
    return merged


@app.get("/blotter")
async def blotter_get(scope=Depends(get_account_scope)):
    engine = get_sync_engine()
    with engine.connect() as conn:
        return fetch_blotter_pg(conn, scope=scope, days=30)


# ---------------------------------------------------------------------------
# Performance — task registry for deduplication (single-worker assumed)
# ---------------------------------------------------------------------------
_running_build: Optional[asyncio.Task] = None


async def _do_performance_rebuild() -> dict:
    """Run portfolio_performance.py and cache result."""
    result = await run_entry_point("xenon-portfolio-perf", ["--json"], timeout=180)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    return result.data


@app.post("/performance")
async def performance_sync():
    """Run portfolio performance metrics. 180s timeout.

    If a build is already in-flight, piggybacks on it (returns same result).
    """
    global _running_build
    if _running_build is not None and not _running_build.done():
        return await _running_build
    _running_build = asyncio.create_task(_do_performance_rebuild())
    return await _running_build


@app.post("/performance/background", status_code=202)
async def performance_background():
    """Fire-and-forget performance rebuild. Returns 202 immediately.

    If a build is already in-flight, returns already_running (no duplicate).
    """
    global _running_build
    if _running_build is not None and not _running_build.done():
        return {"status": "already_running"}
    _running_build = asyncio.create_task(_do_performance_rebuild())
    return {"status": "accepted"}


@app.get("/options/chain")
async def options_chain(symbol: str, expiry: Optional[str] = None):
    """Fetch options chain for a symbol."""
    # Pass the resolved gateway port (4002 paper / 4001 live) — the CLI
    # otherwise defaults to 4001, which fails in paper mode. Mirrors the
    # sync routes (see /sync, /orders/sync).
    args = ["--symbol", symbol.upper(), "--port", str(DEFAULT_GATEWAY_PORT)]
    if expiry:
        args.extend(["--expiry", expiry])
    result = await _run_ib_script_with_recovery("xenon-ib-option-chain", args, timeout=15)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    if result.data and result.data.get("error"):
        raise HTTPException(status_code=502, detail=result.data["error"])
    return result.data


@app.get("/options/expirations")
async def options_expirations(symbol: str):
    """List option expirations for a symbol."""
    result = await run_entry_point(
        "xenon-ib-option-chain",
        ["--symbol", symbol.upper(), "--port", str(DEFAULT_GATEWAY_PORT)],
        timeout=15,
    )
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    if result.data and result.data.get("error"):
        raise HTTPException(status_code=502, detail=result.data["error"])
    return {"symbol": result.data.get("symbol"), "expirations": result.data.get("expirations")}


# ---------------------------------------------------------------------------
# IB Gateway auto-recovery
# ---------------------------------------------------------------------------

_IB_CONN_REFUSED_PATTERNS = (
    "Connect call failed",
    "ECONNREFUSED",
    "Connection refused",
    "TimeoutError",
    "API connection failed",
    "Failed to connect to IB",
    "IBConnectionError",
    "Make sure API port",
    "Connectivity between IBKR and",
    "request timed out",
)

# Cooldown: after an IB subprocess fails with a connection error, skip
# subsequent attempts for this many seconds to avoid churn.
_IB_SCRIPT_COOLDOWN_SECS = 15.0
_ib_last_failure: float = 0.0  # monotonic timestamp of last IB connection failure


def _is_ib_connection_error(error_msg: str) -> bool:
    """Check if an error message indicates IB Gateway is unreachable."""
    return any(p in (error_msg or "") for p in _IB_CONN_REFUSED_PATTERNS)


def _pool_has_any_connection() -> bool:
    """Quick check: does the pool have at least one live IB connection?

    If yes, the Gateway is up and subprocesses should be able to connect.
    If no, the Gateway is likely down — subprocess will also fail.
    """
    if not ib_pool:
        return False
    for role in ("sync", "orders", "data"):
        if ib_pool.is_connected(role):
            return True
    return False


async def _run_ib_script_with_recovery(entry: str, args: list, timeout: float = 30) -> ScriptResult:
    """Run an IB-dependent xenon-* entry point with pre-flight health check and cooldown.

    Three layers of fast-fail:
    1. Cooldown: if a recent IB script failed, skip for _IB_SCRIPT_COOLDOWN_SECS
    2. Pool check: if pool is disconnected, verify Gateway before spawning
    3. Post-failure: verify Gateway health before restarting
    """
    global _ib_last_failure

    # Layer 1: Cooldown — skip if a recent failure occurred
    now = time.monotonic()
    if _ib_last_failure > 0 and (now - _ib_last_failure) < _IB_SCRIPT_COOLDOWN_SECS:
        elapsed = now - _ib_last_failure
        logger.debug(
            "Skipping %s — IB cooldown active (%.1fs since last failure, %ds cooldown)",
            entry,
            elapsed,
            _IB_SCRIPT_COOLDOWN_SECS,
        )
        return ScriptResult(
            ok=False,
            error="IB Gateway connection recently failed. Retrying shortly.",
        )

    # Layer 2: Pre-flight pool check
    if not _pool_has_any_connection():
        gw_status = await check_ib_gateway()
        port_ok = gw_status.get("port_listening", False)
        upstream_dead = gw_status.get("upstream_dead", False)

        if not port_ok or upstream_dead:
            _ib_last_failure = now
            logger.warning(
                "Skipping %s — Gateway down (port=%s, upstream_dead=%s), pool disconnected",
                entry,
                port_ok,
                upstream_dead,
            )
            return ScriptResult(
                ok=False,
                error="IB Gateway is not accepting connections. Check IBKR Mobile for 2FA approval.",
            )

    result = await run_entry_point(entry, args, timeout=timeout)

    # Clear cooldown on success
    if result.ok:
        _ib_last_failure = 0.0

    if not result.ok and _is_ib_connection_error(result.error):
        # Set cooldown to prevent churn from repeated failures
        _ib_last_failure = time.monotonic()

        # Verify Gateway is actually down before restarting
        gw_status = await check_ib_gateway()
        port_ok = gw_status.get("port_listening", False)
        upstream_dead = gw_status.get("upstream_dead", False)

        if port_ok and not upstream_dead:
            # Gateway is healthy — subprocess failed for other reasons
            logger.warning(
                "Script %s failed but Gateway is healthy — not restarting (cooldown %ds)",
                entry,
                _IB_SCRIPT_COOLDOWN_SECS,
            )
            return result

        if is_cloud_mode() or is_docker_mode():
            # Cloud/Docker manages Gateway reliability — don't attempt restart.
            mode = "cloud" if is_cloud_mode() else "Docker"
            logger.warning(
                "IB Gateway unreachable in %s mode (port=%s, upstream_dead=%s) — not restarting (%s handles it)",
                mode,
                port_ok,
                upstream_dead,
                mode,
            )
            msg = f"IB Gateway is not responding ({mode} mode). " + (
                "Check remote host and Tailscale."
                if is_cloud_mode()
                else "Docker will auto-restart the container. Check IBKR Mobile for 2FA approval."
            )
            result = ScriptResult(ok=False, error=msg)
        else:
            logger.warning(
                "IB Gateway unreachable (port=%s, upstream_dead=%s), attempting auto-restart...",
                port_ok,
                upstream_dead,
            )
            gw_result = await restart_ib_gateway()

            if gw_result.get("restarted") and gw_result.get("port_listening"):
                logger.info("IB Gateway restarted, retrying %s", entry)
                _ib_last_failure = 0.0  # Clear cooldown after successful restart
                if ib_pool:
                    await ib_pool.disconnect_all()
                    await ib_pool.connect_all()
                result = await run_entry_point(entry, args, timeout=timeout)
            else:
                logger.error("IB Gateway restart failed: %s", gw_result)
                result = ScriptResult(
                    ok=False,
                    error=f"IB Gateway is down and restart failed. {gw_result.get('error', '')}".strip()
                    + " Check IBKR Mobile for 2FA approval.",
                )

    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import uvicorn

    uvicorn.run(
        "xenon.api.server:app",
        host="127.0.0.1",
        port=8321,
        reload=True,
        reload_dirs=[str(SRC_DIR)],
    )


if __name__ == "__main__":
    main()
