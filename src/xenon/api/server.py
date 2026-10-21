"""Xenon FastAPI server — replaces Python shell-outs from Next.js.

Persistent IB connections, shared UW client, uniform JSON responses.
Port 8321, no auth for local use.

Usage:
    python3 -m uvicorn xenon.api.server:app --host 127.0.0.1 --port 8321 --reload
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
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
from xenon.api.auth import (
    classify_auth,
    validate_auth_config,
)
from xenon.api.guards import (
    ReadOnlyDenied,
    ensure_order_allowed,
    get_account_scope,
    get_broker_scope,
    is_read_only,
    mask_account,
    read_only_403,
    require_mode_verified,
)
from xenon.api.ib_gateway import check_ib_gateway, ensure_ib_gateway, is_cloud_mode, is_docker_mode, restart_ib_gateway
from xenon.api.ib_pool import IBPool, check_client_id_base_mapping
from xenon.utils.ib_connection import TEST_BROKER_GUARD_ENV
from xenon.api.order_owner import BROKER_DISCONNECTED, ORDERS_HALTED, OrderOwner
from xenon.api.routes.historical import router as historical_router
from xenon.api.routes.journal import router as journal_router
from xenon.api.routes.orders import orders_payload_for_scope
from xenon.api.routes.orders import router as orders_router
from xenon.api.routes.performance import router as performance_router
from xenon.api.routes.trades import router as trades_router
from xenon.api.routes.watchlist import router as watchlist_router
from xenon.api.routes.wizard import router as wizard_router
from xenon.api.services import audit_enforce
from xenon.api.services.flex_token import flex_token_status
from xenon.api.subprocess import ScriptResult, run_entry_point, run_module
from xenon.api.ws_ticket import create_ticket, validate_ticket
from xenon.clients.ib_client import DEFAULT_GATEWAY_PORT, pool_client_id
from xenon.db.engine import dispose_engine, get_sync_engine, init_engine
from xenon.db.queries.blotter import blotter_has_trades, fetch_blotter_pg, fetch_futu_blotter, merge_pg_and_flex
from xenon.db.schema import account_snapshots, order_events, order_submissions
from xenon.execution import gate4, ib_order_manage, orders_store, preflight, quote_guard, quote_tokens
from xenon.execution.account_scope import IBAccountSelectionError, resolve_from_app_state, select_ib_account
from xenon.execution import ib_place_order
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
from ib_async import Contract, Option, Stock

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


# Shared state
# ---------------------------------------------------------------------------
ib_pool: Optional[IBPool] = None
# The single order owner (PR-10): every cancel/modify/wizard-protect runs on
# the pool ``orders`` connection through it. None until lifespan starts it.
order_owner: Optional[OrderOwner] = None
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

    # Resolve scope from app.state populated earlier in lifespan. Fail
    # closed: an empty scope would map to broker_account=None, which
    # rehydrate treats as "no account filter" (every account's rows).
    _scope_account_env = getattr(app.state, "trading_mode", None)
    _scope_account = getattr(app.state, "account", None)
    if not _scope_account_env or not _scope_account:
        logger.error("rehydrate skipped: IB account scope unresolved (trading_mode/account empty)")
        return
    if getattr(app.state, "mode_verified", False) is not True:
        logger.error("rehydrate skipped: trading mode not verified for account %r", _scope_account)
        return
    _scope_kwargs = {
        "broker": "IB",
        "account_env": _scope_account_env,
        "broker_account": _scope_account,
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
    if getattr(app.state, "mode_verified", False) is not True:
        logger.error("ib activity poller skipped: trading mode not verified for account %r", _scope_account)
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


def _maybe_start_wizard_risk_alert(*, interval_s: float | None = None) -> None:
    """Start the Wizard Risk Alert task (spec §2.9; replaces the monitor
    daemon's wizard_stop_monitor handler).

    Evaluates PROTECTED wizard sessions and NEVER places, cancels or modifies
    an order. Runs under XENON_READ_ONLY=1 too — the monitor then evaluates
    but never writes. Skipped in test mode (tests opt in by patching
    ``_is_test_mode``), when XENON_WIZARD_RISK_ALERT=0, when the IB pool is
    absent, or when the account scope is unresolved / unverified (same gate as
    ``_maybe_start_activity_poller``). Cadence env: XENON_WIZARD_RISK_ALERT_S.
    """
    from xenon.api.services import wizard_risk_alert as wra

    if _is_test_mode():
        logger.info("test_mode: skipping wizard risk alert")
        return
    flag = os.environ.get("XENON_WIZARD_RISK_ALERT", "").strip().lower()
    if flag in {"0", "false", "no", "off"}:
        logger.info("wizard risk alert disabled (XENON_WIZARD_RISK_ALERT=%s)", flag)
        return
    if ib_pool is None:
        logger.info("wizard risk alert skipped: ib_pool not initialized")
        return
    account_env = getattr(app.state, "trading_mode", None)
    account = getattr(app.state, "account", None)
    if not account_env or not account:
        logger.info("wizard risk alert skipped: scope not resolved")
        return
    if getattr(app.state, "mode_verified", False) is not True:
        logger.error("wizard risk alert skipped: trading mode not verified for account %r", account)
        return
    scope = {"broker": "IB", "account_env": account_env, "broker_account": account}
    if interval_s is None:
        try:
            interval_s = float(os.environ.get("XENON_WIZARD_RISK_ALERT_S", wra.DEFAULT_INTERVAL_S))
        except ValueError:
            interval_s = wra.DEFAULT_INTERVAL_S
    monitor = wra.RiskAlertMonitor(
        list_sessions_fn=lambda: wra.list_alert_sessions(scope),
        quote_fn=wra.build_pool_quote_fn(ib_pool),
    )
    app.state.wizard_risk_alert = monitor
    app.state.wizard_risk_alert_task = asyncio.create_task(wra.risk_alert_loop(monitor, interval_s=interval_s))
    logger.info("wizard risk alert started: interval=%ss read_only=%s", interval_s, is_read_only())


async def _stop_wizard_risk_alert() -> None:
    task = getattr(app.state, "wizard_risk_alert_task", None)
    app.state.wizard_risk_alert_task = None
    app.state.wizard_risk_alert = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


async def _cancel_task(task) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


async def _bounded_cleanup(name: str, aw, seconds: float) -> None:
    """One shutdown step: bounded, logged, never raises."""
    try:
        await asyncio.wait_for(aw, seconds)
    except asyncio.TimeoutError:
        logger.error("shutdown: %s did not finish within %ss; continuing", name, seconds)
    except Exception:  # noqa: BLE001
        logger.exception("shutdown: %s failed", name)


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
        logger.error("ib fills replay skipped: scope not resolved (trading_mode/account empty)")
        return
    if getattr(app.state, "mode_verified", False) is not True:
        logger.error("ib fills replay skipped: trading mode not verified for account %r", _scope_account)
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
    """Return the boot scope account, chosen by ``select_ib_account``.

    ``XENON_BROKER_ACCOUNT`` wins when set and managed by the login; with it
    unset, the login must manage exactly one account. Never
    ``managedAccounts()[0]``. Returns "" when the pool isn't connected (test
    mode, Gateway down, etc.) or no account can be chosen — the lifespan then
    sets ``mode_verified=False`` and order routes refuse. Pulled out as a
    module-level function so tests can monkeypatch it without booting a real
    IB connection.
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
    try:
        return select_ib_account(accounts)
    except IBAccountSelectionError as exc:
        logger.error("IB ACCOUNT SCOPE UNRESOLVED — order routes stay disabled: %s", exc)
        return ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start IB pool and UW client on startup, tear down on shutdown."""
    global ib_pool, order_owner, uw_available

    logger.info(validate_auth_config())  # logs posture; raises if internal token == query key

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

    # A5: refuse an unsafe clientId base ↔ database pairing before anything
    # dials the gateway (base 0 only with core_dev; dev.sh exports 100).
    check_client_id_base_mapping(
        os.environ.get("DATABASE_URL"),
        test_session=os.environ.get(TEST_BROKER_GUARD_ENV) == "1",
    )

    # Ensure IB Gateway is running before connecting pool
    gw_status = await ensure_ib_gateway()
    logger.info("IB Gateway: %s", gw_status)

    # IB pool — starts degraded if Gateway is still down after restart attempt
    ib_pool = IBPool()
    app.state.ib_pool = ib_pool
    pool_status = await ib_pool.connect_all()
    logger.info("IB pool status: %s", pool_status)
    # Order owner + its orders-connection supervisor (A2): the only code that
    # ever reconnects the orders role.
    order_owner = OrderOwner(ib_pool)
    order_owner.start()

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
    elif not account or not verified:
        # Boot writers stamp and filter rows by this account; without a
        # resolved, verified account they would write or reconcile unscoped.
        logger.error(
            "IB account scope unresolved or unverified (account=%r, mode_verified=%s) — "
            "skipping rehydrate, fills replay, and activity poller.",
            account,
            verified,
        )
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

    # Wizard Risk Alert (spec §2.9). Starts in read-only sessions too: it
    # evaluates there but never writes.
    _maybe_start_wizard_risk_alert()

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
        # Shutdown — always runs, even if the app raised. The IB section goes
        # FIRST and in its own try/finally, so no other cleanup (a stalled
        # Postgres LISTEN close, Futu, ...) can delay or skip it. Every await
        # here is bounded; see _SHUTDOWN_* for the budget.
        try:
            if order_owner is not None:
                # stop() refuses admission at once, abandons queued order jobs
                # (zero sends) and cancels the supervisor.
                await _bounded_cleanup("order owner stop", order_owner.stop(), _SHUTDOWN_STEP_S["owner"])
            if ib_pool:
                try:
                    await ib_pool.disconnect_all(timeout=_SHUTDOWN_IB_DISCONNECT_S)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ib_pool.disconnect_all failed: %s", exc)
        finally:
            listener = getattr(app.state, "journal_auto_import", None)
            if listener is not None:
                await _bounded_cleanup("journal auto-import listener", listener.stop(), _SHUTDOWN_STEP_S["listener"])
            await _bounded_cleanup(
                "ib activity poller",
                _cancel_task(getattr(app.state, "ib_activity_poller_task", None)),
                _SHUTDOWN_STEP_S["poller"],
            )
            await _bounded_cleanup("wizard risk alert", _stop_wizard_risk_alert(), _SHUTDOWN_STEP_S["wizard"])
            await _bounded_cleanup(
                "futu history loop",
                _cancel_task(getattr(app.state, "futu_history_loop_task", None)),
                _SHUTDOWN_STEP_S["futu_loop"],
            )
            if _futu_client is not None:
                await _bounded_cleanup(
                    "FutuClient disconnect", asyncio.to_thread(_futu_client.disconnect), _SHUTDOWN_STEP_S["futu"]
                )
            await _bounded_cleanup("engine dispose", dispose_engine(), _SHUTDOWN_STEP_S["engine"])
        logger.info("Xenon API shut down")


app = FastAPI(title="Xenon API", version="1.0.0", lifespan=lifespan)


@app.exception_handler(ReadOnlyDenied)
async def _read_only_denied_handler(request: Request, exc: ReadOnlyDenied):
    return read_only_403()


app.include_router(historical_router)
app.include_router(journal_router)
app.include_router(orders_router)
app.include_router(performance_router)
app.include_router(trades_router)
app.include_router(watchlist_router)
app.include_router(wizard_router)

# Read-only write guard — must stay INNER relative to auth_middleware (defined
# below) so unauthenticated requests still get 401 first. Under
# XENON_READ_ONLY=1 it refuses order/journal/wizard writes before FastAPI
# decodes the body — malformed JSON would otherwise 422 ahead of the
# route-level guards.
_READ_ONLY_WRITE_PATHS = frozenset(
    {
        "/orders/place",
        "/orders/cancel",
        "/orders/modify",
        "/orders/replace",
        "/journal",
        "/wizard/plan",
        "/wizard/sessions",
    }
)
# POST /orders/refresh is deliberately absent: under read-only it serves the
# PG view with 200 (no subprocess, no write).
_WIZARD_SESSION_ACTIONS = frozenset({"submit", "reprice", "abort", "protect"})


def _is_write_path(path: str) -> bool:
    # Trailing-slash forms ("/journal/", ".../protect/") would otherwise
    # fall through to the router's 307 redirect instead of the refusal.
    normalized = path.rstrip("/") or "/"
    if normalized in _READ_ONLY_WRITE_PATHS:
        return True
    if normalized.startswith("/wizard/sessions/"):
        return normalized.rsplit("/", 1)[-1] in _WIZARD_SESSION_ACTIONS
    return False


@app.middleware("http")
async def read_only_write_guard(request: Request, call_next):
    if (
        is_read_only()
        and request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and _is_write_path(request.url.path)
    ):
        return read_only_403()
    return await call_next(request)


# Auth middleware — only /health is exempt. /docs and /openapi.json are
# deliberately NOT exempt: they expose the full trading API schema.
# /ws-ticket/validate is NOT exempt either: the Node relay authenticates with
# X-Internal-Token, so an external caller cannot burn or probe tickets.
AUTH_EXEMPT_PATHS = {
    "/health",
    # /dev/rehydrate/synthetic intentionally NOT listed: gated by _dev_probes_enabled()
    # AND now by classify_auth (fail-closed). Exempting it would risk a public write
    # endpoint if DEV_PROBES=1 leaked into a non-local env.
}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Authorize via classify_auth (fail-closed, credential-only)."""
    if request.url.path in AUTH_EXEMPT_PATHS:
        return await call_next(request)

    decision = classify_auth(request)

    if decision.action == "pass":
        request.state.user = decision.identity
        return await call_next(request)

    return JSONResponse(
        status_code=401,
        content={"detail": "Authentication required"},
    )


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
# Health
# ---------------------------------------------------------------------------


def _compute_futu_health() -> dict[str, Any]:
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
    """Every UNKNOWN row in scope (no age window): an unresolved uncertain
    place keeps its Gate-4 reservation until an operator or the orderRef
    recovery resolves it, so it must stay visible however old it is.

    `stale_pending_count` counts PENDING rows older than the rehydrate
    PENDING timeout: a place that crashed or lost its DB write after IB
    may have accepted it. The activity poller binds such a row only from
    broker evidence (orderRef); a row without evidence is counted here and
    never changed (spec §2.6)."""
    from xenon.execution.single_leg_rehydrate import PENDING_TIMEOUT_SECONDS

    try:
        engine = get_sync_engine()
        stale_before = datetime.now(timezone.utc) - timedelta(seconds=PENDING_TIMEOUT_SECONDS)
        with engine.connect() as conn:
            stmt = select(
                func.count().filter(order_submissions.c.state == "UNKNOWN"),
                func.min(order_submissions.c.submitted_at).filter(order_submissions.c.state == "UNKNOWN"),
                func.count().filter(
                    order_submissions.c.state == "PENDING", order_submissions.c.submitted_at < stale_before
                ),
            ).where(order_submissions.c.state.in_(("UNKNOWN", "PENDING")))
            if scope is not None:
                stmt = stmt.where(
                    order_submissions.c.broker == scope["broker"],
                    order_submissions.c.account_env == scope["account_env"],
                    order_submissions.c.broker_account == scope["broker_account"],
                )
            unknown_count, oldest, stale_pending = conn.execute(stmt).one()
    except Exception:
        logger.warning("[health] failed to load order submissions health", exc_info=True)
        return {
            "unknown_count": None,
            "oldest_unknown_age_seconds": None,
            "stale_pending_count": None,
            "alarm": False,
        }

    count = int(unknown_count or 0)
    oldest_age = None
    if oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        oldest_age = max(0, int((datetime.now(timezone.utc) - oldest).total_seconds()))
    return {
        "unknown_count": count,
        "oldest_unknown_age_seconds": oldest_age,
        "stale_pending_count": int(stale_pending or 0),
        "alarm": count > 5,
    }


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
                        # The Gate-4 invalidation watermark is a sentinel row,
                        # not a background writer — never shown as one.
                        service_health.c.service != gate4.VIEW_WATERMARK_SERVICE,
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
    owner = order_owner.status() if order_owner is not None else None
    return {
        # Stays HTTP 200 (a failing healthcheck restart would clear the latch);
        # a latched ORDERS_HALTED turns the status red.
        "status": "orders_halted" if owner and owner["halted"] else "ok",
        "test_mode": _is_test_mode(),
        "ib_gateway": gw,
        "ib_pool": ib_pool.status() if ib_pool else {},
        "orders_owner": owner,
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
        "flex_token": flex_token_status(),
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
        "flex_token": flex_token_status(),
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
async def get_ws_ticket(request: Request):
    """Issue a short-lived ticket for WebSocket authentication.

    The middleware already authorized this request (internal token or scoped
    API key) and set request.state.user. Reading it here avoids re-validating.
    """
    user = getattr(request.state, "user", None) or {}
    ticket = create_ticket(user.get("sub", "unknown"))
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


@app.post("/portfolio/sync")
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
    if age is not None and age > _STALE_SNAPSHOT_AFTER_SYNC_S and not _is_test_mode():
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


@app.post("/portfolio/background-sync", status_code=202)
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
    try:
        result = await _run_ib_script_with_recovery(
            "xenon-ib-sync", ["--sync", "--port", str(DEFAULT_GATEWAY_PORT)], timeout=30
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
    scope = _resolve_scope_kwargs()
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
    if is_read_only():
        return "read_only"
    if order_owner is None:
        return "owner_unavailable"
    if order_owner.halted:
        return "owner_halted"
    try:
        denial = ensure_order_allowed("naked-short audit cancel", app.state)
    except HTTPException:
        return "mode_unverified"
    if denial is not None:
        return f"order_denied:{_json_body(denial).get('reason_code')}"
    return None


async def _audit_cancel(target: dict) -> str:
    """Cancel one audit target by its full identity; permId is always set,
    so the core never falls back to orderId."""
    kind, resp = await _cancel_order(
        {"orderId": target["order_id"], "permId": target["perm_id"], "clientId": target["client_id"]},
        reason_code=audit_enforce.REASON_CODE,
    )
    if kind == "ok":
        return audit_enforce.CONFIRMED
    detail = _json_body(resp).get("detail") or {}
    upstream = detail.get("upstream") if isinstance(detail, dict) else None
    logger.warning("naked short audit cancel %s for %s: %s", kind, target, detail)
    if kind == "failed" and isinstance(upstream, dict) and upstream.get("code") == 10147:
        return audit_enforce.NOT_FOUND  # permId gone: nothing cancelled
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

    client = _get_futu_client()

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


@app.post("/orders/refresh")
async def orders_refresh(request: Request, scope=Depends(get_broker_scope)):
    """Sync orders into Postgres, then return the scoped payload.

    IB: subprocess pull (auto-allocated clientId 20-49, gateway auto-restart on
    ECONNREFUSED), gated on IB mode verification.
    FUTU: read-only pull from OpenD into futu_orders/trades/closed_trades + journal
    via sync_futu_orders (DB-first; no-ops under XENON_READ_ONLY=1). The GET read
    path stays Postgres-only.
    """
    # Read-only sessions get the PG view directly — no --sync subprocess, no
    # OpenD connect, no writes. Placed before the mode check and the test-mode
    # short-circuit so the single contract holds under every flag combination.
    if is_read_only():
        return orders_payload_for_scope(scope)

    # IB requires a verified trading mode before any work — including the
    # test-mode short-circuit — so a mode mismatch is refused even in test mode
    # (preserves the /orders/refresh guard contract). FUTU is read-only and
    # carries no IB trading mode to verify, so it is exempt.
    if scope.broker != "FUTU":
        require_mode_verified(request)

    if _is_test_mode():
        return {"status": "ok", "orders": []}

    if scope.broker == "FUTU":
        await _futu_refresh_db_first(scope)
        return orders_payload_for_scope(scope)

    # IB path — mode already verified above; touch the gateway.
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
        currency=str(body.get("currency") or "USD").upper(),
        exchange=body.get("exchange"),
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


# TIFs the place flow supports end to end (web OrderTif / placeOrderBodySchema;
# the place CLI passes the value to IB LimitOrder unchanged).
_PLACE_TIFS = frozenset({"DAY", "GTC"})


def _canonical_tif(raw) -> str:
    """Strip + upper; None / "" → DAY (the CLI default). Raises ValueError
    for anything outside _PLACE_TIFS."""
    if raw is None:
        return "DAY"
    if not isinstance(raw, str):
        raise ValueError(f"tif must be one of {sorted(_PLACE_TIFS)}")
    tif = raw.strip().upper() or "DAY"
    if tif not in _PLACE_TIFS:
        raise ValueError(f"tif must be one of {sorted(_PLACE_TIFS)}, got {raw!r}")
    return tif


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


def _universe_precheck(body: dict) -> Verdict | None:
    """Pure-metadata Gate-4 checks that must precede the reservation.

    Universe/index rules are request-shape validation — no portfolio data
    needed — so they run before the advisory-locked coverage transaction.
    """
    from xenon.execution.universe import is_index, is_known

    ticker = str(body.get("symbol", "")).upper()
    currency = str(body.get("currency") or "USD").upper()
    # Non-USD cash equities sit outside the V1 universe by definition; they
    # gate on currency instead (same carve-out as the legacy preflight).
    is_foreign_equity = body.get("type") == "stock" and currency != "USD"
    if not is_foreign_equity and not is_known(ticker):
        return Verdict(
            accept=False,
            reason_code=ReasonCode.UNIVERSE_UNKNOWN,
            reason_detail=f"{ticker} not in V1 universe",
        )
    # is_index() raises KeyError for non-universe tickers — the is_known
    # early return above guarantees this only runs for known USD tickers.
    if body.get("type") == "stock" and not is_foreign_equity and is_index(ticker):
        return Verdict(
            accept=False,
            reason_code=ReasonCode.INDEX_HAS_NO_STOCK,
            reason_detail=f"{ticker} is an index — no stock leg exists",
        )
    return None


def _preflight_block_response(verdict: Verdict) -> JSONResponse:
    code = verdict.reason_code.value if verdict.reason_code else None
    # `detail` is the field web/lib/xenonApi.ts:39 reads for human-readable
    # error copy; the reason_code fields drive structured UI toast mapping.
    return JSONResponse(
        status_code=400,
        content={
            "detail": verdict.reason_detail or code or "Preflight blocked",
            "reason_code": code,
            "reason_detail": verdict.reason_detail,
        },
    )


# How long a cancel/modify request waits for the order owner's worker. After
# it the request answers (unsent if the job never started, else unconfirmed)
# and a started job keeps running and settles Gate 4 itself. Gate 4 expiry
# does not depend on it: the send_by fence is checked right before the send.
_ORDER_ACTION_TIMEOUT_S = 15


def _gate4_reject_verdict(
    reasons: list[str],
    uncovered: dict | None,
    short_stock: dict | None,
    split_required: bool = False,
) -> Verdict:
    """Map a coverage rejection to the route's reason-code contract.

    Infrastructure failures (view/scope) take precedence — a classifiable
    coverage breach computed on unprovable data is still reported, but the
    integrity problem is the headline. `split_required` marks a guaranteed
    BAG that only fails leg-by-leg — the operator sends the legs as
    separate orders, BUY leg first.
    """
    from xenon.execution.universe import is_index

    if "view_incomplete" in reasons:
        return Verdict(
            accept=False,
            reason_code=ReasonCode.PORTFOLIO_SNAPSHOT_REQUIRED,
            reason_detail="Portfolio snapshot required to verify short exposure",
        )
    if "view_stale" in reasons:
        return Verdict(
            accept=False,
            reason_code=ReasonCode.PORTFOLIO_SNAPSHOT_STALE,
            reason_detail="Portfolio snapshot is stale; sync before submitting SELL exposure",
        )
    if any(r.startswith("candidate_unknown_metadata") and r.rsplit(":", 1)[-1] in ("mult", "tc") for r in reasons):
        # The candidate's contract could not be qualified at IB (conId /
        # tradingClass / multiplier / underlying type) — never guessed.
        # Other leg problems (right/action/exp/…) are body defects and fall
        # through to INVALID_ORDER_BODY below.
        return Verdict(
            accept=False,
            reason_code=ReasonCode.GATE4_UNRESOLVED,
            reason_detail="Contract metadata unavailable from IB; cannot verify cover. " + "; ".join(reasons),
        )
    if split_required:
        return Verdict(
            accept=False,
            reason_code=ReasonCode.COMBO_SPLIT_REQUIRED,
            reason_detail="Combo legs are checked one by one while netting is off. Send the BUY leg as its own order first; send the SELL leg after the BUY fills and the portfolio syncs.",
        )
    if short_stock:
        sym = sorted(short_stock)[0]
        return Verdict(
            accept=False,
            reason_code=ReasonCode.INSUFFICIENT_SHARES,
            reason_detail=f"Insufficient shares to cover stock sell in {sym}",
        )
    if uncovered:
        key = sorted(uncovered, key=str)[0]
        sym = key[0] if isinstance(key, tuple) else str(key)
        is_idx = False
        try:
            is_idx = bool(is_index(sym))
        except KeyError:
            is_idx = False
        code = ReasonCode.INDEX_CALL_UNCOVERED if is_idx else ReasonCode.ETF_CALL_UNCOVERED
        return Verdict(
            accept=False,
            reason_code=code,
            reason_detail=f"Uncovered short call on {sym}",
        )
    if any(r.startswith("candidate_") for r in reasons):
        return Verdict(
            accept=False,
            reason_code=ReasonCode.INVALID_ORDER_BODY,
            reason_detail="; ".join(reasons),
        )
    return Verdict(
        accept=False,
        reason_code=ReasonCode.GATE4_UNRESOLVED,
        reason_detail="; ".join(reasons) or "Gate-4 could not prove coverage",
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
    # Stock quote/qualify path: honor body exchange/currency so a foreign cash
    # equity (5016 TSEJ JPY, 000660 KRX KRW) quotes from its native venue.
    # Option branch stays SMART/USD (US/index options only — no foreign options).
    return Stock(
        symbol,
        str(body.get("exchange") or "SMART").upper(),
        str(body.get("currency") or "USD").upper(),
    )


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
    body = await request.json()
    return await _orders_place_from_body(body)


def _gate4_leg_specs(body: dict) -> list[dict]:
    """One spec per option leg the gate must qualify (combo legs in body
    order; a single option body is one leg)."""
    sym = str(body.get("symbol", "")).upper()
    raw_legs = body.get("legs") or [] if body.get("type") == "combo" else [body]
    specs = []
    for leg in raw_legs:
        con_id = leg.get("con_id") or leg.get("conId")
        try:
            strike = float(leg.get("strike")) if leg.get("strike") is not None else None
        except (TypeError, ValueError):
            strike = None
        specs.append(
            {
                "sym": sym,
                "right": str(leg.get("right") or "").upper() or None,
                "exp": str(leg.get("expiry") or "").replace("-", "") or None,
                "strike": strike,
                "con_id": int(con_id) if str(con_id or "").isdigit() and int(con_id) > 0 else None,
            }
        )
    return specs


def _resolve_leg_meta_with_client(client: Any, specs: list[dict]) -> list[dict | None]:
    """Qualify each leg via reqContractDetails; a leg whose qualified contract
    does not match the body (symbol/right/expiry/strike) resolves to None."""
    from xenon.execution import contract_meta

    _ensure_thread_event_loop()
    out: list[dict | None] = []
    for spec in specs:
        if spec["con_id"] is not None:
            contract = Contract(conId=spec["con_id"])
        elif spec["right"] and spec["exp"] and spec["strike"] is not None:
            contract = Option(
                symbol=spec["sym"],
                lastTradeDateOrContractMonth=spec["exp"],
                strike=spec["strike"],
                right=spec["right"],
                exchange="SMART",
                currency="USD",
            )
        else:
            out.append(None)
            continue
        meta = contract_meta.fetch_meta(client, contract)
        if (
            meta is None
            or meta["sec"] != "OPT"
            or meta["sym"] != spec["sym"]
            or meta["right"] != spec["right"]
            or meta["exp"] != spec["exp"]
            or spec["strike"] is None
            or meta["strike"] is None
            or abs(meta["strike"] - spec["strike"]) > 1e-6
        ):
            out.append(None)
            continue
        out.append(meta)
    return out


async def _resolve_gate4_leg_meta(body: dict) -> list[dict | None]:
    """IB-qualified option metadata for the Gate-4 candidate (spec §2.3 cover
    unit). Any failure leaves the leg unqualified → the gate fails closed."""
    specs = _gate4_leg_specs(body)
    pool = ib_pool
    if pool is None or not specs:
        return [None] * len(specs)
    try:
        async with pool.acquire("data") as client:
            return await pool.run_sync("data", _resolve_leg_meta_with_client, client, specs)
    except Exception as exc:  # noqa: BLE001 — fail closed, never guess metadata
        logger.warning("gate4 contract qualification failed: %s", exc)
        return [None] * len(specs)


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


async def _orders_place_from_body(body: dict):
    denial = ensure_order_allowed("order placement", app.state)
    if denial is not None:
        return denial
    prep = await _prepare_place(body)
    if isinstance(prep, JSONResponse):
        return prep
    cid, req_row, candidate, legs_payload, scope = prep
    outcome = gate4.reserve_with_coverage(
        "local",
        cid,
        req_row,
        candidate=candidate,
        legs_payload=legs_payload,
        scope=scope,
        override_audit=None,
    )
    refusal = _reservation_refusal(outcome, cid, replacement=False)
    if refusal is not None:
        return refusal
    return await _place_reserved(body, outcome.submission_id, scope)


async def _prepare_place(body: dict):
    """Pre-reservation place checks → JSONResponse refusal, or
    (cid, request row, Gate-4 candidate, legs payload, scope)."""
    # RegimeGate (VCG-CRI signal layer) was removed in the pure-portfolio
    # pivot (#104). Order entry no longer gates on regime tiers and there is
    # no override audit.
    # Gate 4 (PR-03b): universe/index precheck — pure request-shape rules.
    verdict = _universe_precheck(body)
    if verdict is not None:
        return _preflight_block_response(verdict)

    # Gate 4 (PR-03b): coverage.evaluate + reservation insert in ONE
    # transaction serialized by an account-scope pg advisory lock. The SELL
    # short-circuit is gone — every non-BUY candidate (and every combo,
    # either envelope) is evaluated against the durable view. A single-leg
    # BUY still skips evaluation (it creates no demand) but still serializes
    # its reservation under the same lock.
    cid = body.get("client_attempt_id")
    if not cid:
        return JSONResponse(
            status_code=400,
            content={"detail": "client_attempt_id is required"},
        )
    scope = _resolve_scope_kwargs()
    security_type = "STK" if body.get("type") == "stock" else "BAG" if body.get("type") == "combo" else "OPT"
    ticker = str(body.get("symbol", "")).upper()
    try:
        # One canonical TIF for the reservation, the stored column and the
        # place CLI (PR-03b round 7). Unknown values fail before reserving.
        tif = _canonical_tif(body.get("tif"))
        body["tif"] = tif
        req_row = orders_store.RequestRow(
            ticker=ticker,
            security_type=security_type,
            action=str(body.get("action", "")).upper(),
            quantity=int(body.get("quantity", 0)),
            expiry=body.get("expiry"),
            strike=Decimal(str(body["strike"])) if body.get("strike") is not None else None,
            right=(body.get("right") or "").upper() or None,
            # Multiplier is server-authoritative — a client-provided value
            # must never reach the reservation row (spoofed mult inflates
            # share-cover math).
            multiplier=gate4.server_multiplier(ticker),
            con_id=int(body.get("con_id") or 0) or None,
            limit_price=Decimal(str(body.get("limitPrice", "0"))),
            tif=tif,
        )
    except (ValueError, TypeError, ValidationError) as exc:
        return _preflight_block_response(_invalid_order_body_response(exc))
    leg_meta = await _resolve_gate4_leg_meta(body) if gate4.needs_contract_meta(body) else None
    candidate, legs_payload = gate4.candidate_from_place_body(body, scope["broker_account"], leg_meta)
    return cid, req_row, candidate, legs_payload, scope


def _reservation_refusal(outcome, cid: str, *, replacement: bool) -> JSONResponse | dict | None:
    """Map a non-winner reservation outcome to its response (None = winner).

    `replacement`: a claim for /orders/replace — any existing attempt id is a
    refusal (nothing cancelled), never a 200 the client reads as replaced."""
    if outcome.status == "target_unresolved":
        return JSONResponse(
            status_code=409,
            content={
                "reason_code": ReasonCode.ORDER_TARGET_AMBIGUOUS.value,
                "detail": f"{outcome.detail} — nothing was cancelled",
            },
        )
    if outcome.status == "in_progress":
        return JSONResponse(
            status_code=409,
            content={
                "reason_code": ReasonCode.REPLACEMENT_IN_PROGRESS.value,
                "detail": "Another replacement of this order is in progress or done — nothing was cancelled. Check working orders.",
                "state": outcome.state,
                "submission_id": outcome.submission_id,
            },
        )
    if outcome.status == "rejected":
        return _preflight_block_response(
            _gate4_reject_verdict(
                outcome.reasons, outcome.uncovered, outcome.short_stock, outcome.split_required
            )
        )
    if outcome.status == "body_mismatch" and outcome.reason_code == ReasonCode.ATTEMPT_ALREADY_PLACED.value:
        # Same id, different body, and that attempt may be at IB: the new
        # terms were not placed. Never invite a rebuild/resubmit (PR-08).
        return JSONResponse(
            status_code=409,
            content={
                "reason_code": ReasonCode.ATTEMPT_ALREADY_PLACED.value,
                "detail": f"attempt {cid} belongs to an existing order; the requested terms are not confirmed",
                "state": outcome.state,
                "submission_id": outcome.submission_id,
            },
        )
    if outcome.status == "body_mismatch":
        return JSONResponse(
            status_code=409,
            content={
                "reason_code": ReasonCode.ATTEMPT_BODY_MISMATCH.value,
                "detail": f"attempt {cid} already reserved with a different request body",
                "submission_id": outcome.submission_id,
            },
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
    if outcome.status == "duplicate" and outcome.state in ("UNKNOWN", "PENDING"):
        # Same attempt id while the first send is unresolved / in flight:
        # never a 200 the client reads as success (it would mint a new id).
        return JSONResponse(
            status_code=502,
            content={
                **_PLACE_UNKNOWN_BODY,
                "state": outcome.state,
                "submission_id": outcome.submission_id,
                "duplicate_of": outcome.duplicate_of,
            },
        )
    if outcome.status == "duplicate" and replacement:
        return JSONResponse(
            status_code=409,
            content={
                "reason_code": ReasonCode.ATTEMPT_BODY_MISMATCH.value,
                "detail": f"attempt {cid} already used (state {outcome.state}) — nothing was cancelled",
                "state": outcome.state,
                "submission_id": outcome.submission_id,
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
    return None


async def _place_reserved(body: dict, submission_id: str, scope: dict):
    """Quote gate → broker place → verdict for a row already reserved PENDING."""
    # F3: quote-token + tick-grid + limit-band + market-hours. Runs AFTER
    # the reservation so a Gate-4 coverage reason always wins over a quote
    # failure; a quote rejection marks the row REJECTED so the reservation
    # releases its cover instead of leaking a PENDING row.
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
                orders_store.mark_terminal(
                    submission_id=submission_id,
                    state="REJECTED",
                    reason_code=qv.reason_code.value if qv.reason_code else "QUOTE_REJECT",
                    filled_qty=0,
                    avg_fill_price=None,
                    expected_states=("PENDING",),
                )
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

    if _override_detail is not None:
        orders_store.record_event(submission_id, "PREFLIGHT_ACK_LIMIT", _override_detail)

    if _is_test_mode():
        order_id, perm_id = _next_test_order_ids()
        orders_store.mark_submitted(
            submission_id=submission_id,
            ib_order_id=str(order_id),
            perm_id=str(perm_id),
            placing_client_id=pool_client_id("orders"),
            expected_states=("PENDING",),
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

    # Place on the account the reservation row was stamped with — never on an
    # account named by the request body, never on IB's default. The core
    # fails closed if it is legacy_unknown or not managed. submission_id
    # becomes orderRef "xo:<submission_id>" (recovery key); a body-supplied
    # orderRef is dropped.
    params = {
        **{k: v for k, v in body.items() if k != "orderRef"},
        "account": scope["broker_account"],
        "submission_id": submission_id,
    }
    if order_owner is None:
        return _finish_place(submission_id, _owner_unavailable_reply())
    settled: dict = {}

    def settle(reply: dict) -> None:
        # Runs exactly once: on the orders worker for a started job (even
        # when this request is gone), else here for a job that never started.
        settled["response"] = _finish_place(submission_id, reply)

    await order_owner.run(
        lambda client: ib_place_order.place_order_on_client(client, params),
        timeout=_PLACE_TIMEOUT_S,
        settle=settle,
    )
    if "response" in settled:
        return settled["response"]
    # Deadline after the job started: IB may hold the order. The worker
    # still owns the final write (PENDING / UNKNOWN → WORKING / REJECTED).
    logger.warning("place: deadline after start, outcome pending on the worker: submission=%s", submission_id)
    return _place_outcome_unknown(submission_id, {})


# Owner classifications that prove nothing was sent → reason code on the
# FAILED row and the 503 body (a known 5xx code is terminal in the web).
_PLACE_NOT_SENT_REASONS = {
    "connection": ReasonCode.IB_CONNECTION,
    BROKER_DISCONNECTED: ReasonCode.IB_BROKER_DISCONNECTED,
    ORDERS_HALTED: ReasonCode.ORDERS_HALTED,
}


def _owner_unavailable_reply() -> dict:
    return {
        "status": "error",
        "message": "Order owner is not running; nothing was sent",
        "classification": BROKER_DISCONNECTED,
        "amend_outcome": "not_sent",
    }


def _finish_place(submission_id: str, reply: dict) -> Any:
    """Persist one place outcome and build its response (A3). Runs on the
    orders worker for a started job, so it never depends on the request.

    - owner refusal / abandoned before start (``amend_outcome: not_sent``)
      → FAILED (from PENDING or UNKNOWN), 503 with IB_CONNECTION /
      IB_BROKER_DISCONNECTED / ORDERS_HALTED: nothing was sent;
    - possibly sent, not confirmed (``amend_outcome: unconfirmed``) →
      UNKNOWN with the ids the core learned (PENDING or UNKNOWN), 502;
    - otherwise the core's result through the verdict. Final writes accept
      PENDING or UNKNOWN (the request may have marked UNKNOWN at its
      deadline) and never overwrite a state the poller set from broker
      evidence. The placing clientId stored is always the owner's.
    """
    owner_id = pool_client_id("orders")
    outcome = reply.get("amend_outcome")
    if outcome == "not_sent" and reply.get("classification") in _PLACE_NOT_SENT_REASONS:
        reason = _PLACE_NOT_SENT_REASONS[reply["classification"]].value
        # PENDING or UNKNOWN: the request may have marked UNKNOWN at its
        # deadline before the worker proved nothing was sent. Never a state
        # the poller set from broker evidence.
        applied = orders_store.mark_terminal(
            submission_id=submission_id,
            state="FAILED",
            reason_code=reason,
            filled_qty=0,
            avg_fill_price=None,
            expected_states=_PLACE_FINAL_FROM,
        )
        if not applied:
            # Stronger evidence won the row: do not claim FAILED.
            logger.warning(
                "place: proven unsent (%s) but submission %s is no longer PENDING/UNKNOWN; row left as is",
                reason,
                submission_id,
            )
            return JSONResponse(status_code=502, content={**_PLACE_UNKNOWN_BODY, "submission_id": submission_id})
        return JSONResponse(status_code=503, content={"detail": reply.get("message"), "reason_code": reason})
    if outcome == "unconfirmed":
        logger.warning("place: possibly sent, unconfirmed: submission=%s (%s)", submission_id, reply.get("message"))
        return _place_outcome_unknown(submission_id, {**reply, "clientId": owner_id}, from_states=_PLACE_FINAL_FROM)
    return _place_result(submission_id, reply, owner_id)


# States a worker's final place write may leave: PENDING, or UNKNOWN set by
# the request at its deadline. Never a broker-observed or terminal state.
_PLACE_FINAL_FROM = ("PENDING", "UNKNOWN")


def _place_result(submission_id: str, data: dict, owner_id: int) -> Any:
    """Verdict + persistence for a place-core result."""
    final_states = _PLACE_FINAL_FROM
    verdict = _place_verdict(data)
    if verdict == "unknown":
        return _place_outcome_unknown(submission_id, {**data, "clientId": owner_id}, from_states=final_states)
    if verdict == "rejected":
        # B6 — write a reason_code the UI toast map can resolve. Most IB
        # rejections collapse to IB_REJECT, but tick-rule rejections
        # (code 110) re-map to LIMIT_OFF_TICK so the user sees a clean
        # actionable message instead of raw IB error text. The raw IB
        # numeric code + message always go into the orders_events audit
        # row so we don't lose the info.
        ib_code = data.get("code")
        ib_message = data.get("message", "Order failed")
        core_reason = data.get("reason_code")
        if core_reason in (
            ReasonCode.ACCOUNT_UNRESOLVED.value,
            ReasonCode.ACCOUNT_NOT_MANAGED.value,
            ReasonCode.INVALID_ORDER_BODY.value,
        ):
            reason_code = core_reason
        elif str(ib_code) == "110":
            reason_code = ReasonCode.LIMIT_OFF_TICK.value
            logger.warning(
                "IB rejected order with tick-rule violation: submission=%s ib_code=110 ib_message=%s",
                submission_id,
                ib_message,
            )
        else:
            reason_code = ReasonCode.IB_REJECT.value
        # Conditional: a poller that already saw this order at IB wins, and
        # then the core's "rejected" is contradicted by broker evidence.
        if not orders_store.mark_terminal(
            submission_id=submission_id,
            state="REJECTED",
            reason_code=reason_code,
            filled_qty=0,
            avg_fill_price=None,
            expected_states=final_states,
        ):
            logger.warning("place: core rejected but row %s already moved on; not rejecting", submission_id)
            return JSONResponse(status_code=502, content={**_PLACE_UNKNOWN_BODY, "submission_id": submission_id})
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
        # Top-level reason_code so the toast helper renders the mapped copy.
        return JSONResponse(
            status_code=502,
            content={"detail": ib_message, "reason_code": reason_code},
        )
    if data.get("clientId") not in (None, owner_id):
        logger.warning("place: core reported clientId %s, owner is %s", data.get("clientId"), owner_id)
    data = {**data, "clientId": owner_id}
    try:
        # PENDING / UNKNOWN only: the activity poller may already have bound
        # (WORKING) or finished (FILLED) this row from broker evidence;
        # never demote it.
        orders_store.mark_submitted(
            submission_id=submission_id,
            ib_order_id=str(data.get("orderId") or ""),
            perm_id=str(data.get("permId") or ""),
            placing_client_id=owner_id,
            expected_states=final_states,
        )
    except Exception as exc:
        # IB accepted the order: report it (never a 5xx that invites a
        # resend). The row stays PENDING; orderRef recovery repairs it.
        logger.error("place: IB accepted but persist failed: submission=%s", submission_id, exc_info=True)
        _record_place_event(submission_id, {"persist_error": str(exc)})
        return {
            **data,
            "submission_id": submission_id,
            "persist_warning": "Order accepted by IB but not saved; it will be reconciled from IB.",
        }
    if data.get("ibNotices"):
        _record_place_event(submission_id, {"notices": data["ibNotices"]})
    return data


def _place_verdict(data: dict | None) -> str:
    """``accepted`` | ``rejected`` | ``unknown`` for a place-core result.

    The core's explicit ``outcome`` (from the ordered ib_async Trade.log) is
    trusted: ``accepted`` / ``unconfirmed`` → accepted (stored WORKING),
    ``rejected`` → rejected; ``status: uncertain`` → unknown. Every result
    after ``placeOrder`` carries an outcome or ``uncertain``, so a
    ``status: error`` without one is a pre-send error → rejected.

    A WORKING row is then updated by the activity poller / fill mirror, but
    the activity-mirror sweep (``sweep_disappeared_orders``) skips an empty
    open-order snapshot and needs two consecutive misses, and boot rehydrate
    without a positions baseline leaves the row as is — a per-row grace /
    unknown-order reconciliation is a follow-up.
    """
    if not data:
        return "unknown"
    outcome = data.get("outcome")
    if data.get("status") == "uncertain" or outcome == "uncertain":
        return "unknown"
    if outcome == "rejected":
        return "rejected"
    if outcome in ("accepted", "unconfirmed") or data.get("status") != "error":
        return "accepted"
    return "rejected"


_PLACE_UNKNOWN_BODY = {
    "detail": "Order outcome unknown — check working orders before placing it again. Do not resubmit.",
    "reason_code": ReasonCode.PLACE_OUTCOME_UNKNOWN.value,
}


def _place_outcome_unknown(
    submission_id: str, data: dict, from_states: tuple[str, ...] = ("PENDING",)
) -> JSONResponse:
    """IB may or may not hold the order. Never leave the row PENDING (it
    would age into a rehydrate decision) and never call it rejected. UNKNOWN
    is the uncertain state /health counts; it keeps the Gate-4 reservation.
    Any IB ids the CLI returned are stored in the same conditional write
    (PENDING → UNKNOWN only: a row the poller already bound or filled from
    broker evidence is left as is). The activity poller resolves the row by
    orderRef "xo:<submission_id>". A DB failure still returns the uncertain
    502 (the row then stays PENDING, which keeps the reservation too).

    The request's deadline write is PENDING-only and carries no ids. The
    worker's late uncertain write passes ``from_states`` PENDING/UNKNOWN: it
    enriches the row with the ids it learned and the owner id (non-empty
    values only, so neither write ever clears the other's evidence)."""
    try:
        applied = orders_store.mark_place_unknown(
            submission_id=submission_id,
            ib_order_id=str(data.get("orderId") or "") or None,
            perm_id=str(data.get("permId") or "") or None,
            placing_client_id=int(data["clientId"]) if data.get("clientId") is not None else None,
            reason_code=ReasonCode.PLACE_OUTCOME_UNKNOWN.value,
            expected_states=from_states,
        )
        if applied and (data.get("orderId") or data.get("permId") or data.get("code")):
            _record_place_event(
                submission_id,
                {
                    "ib_code": data.get("code"),
                    "ib_message": data.get("message"),
                    "initial_status": data.get("initialStatus"),
                },
            )
    except Exception:
        logger.error("place: failed to persist UNKNOWN for submission %s", submission_id, exc_info=True)
    return JSONResponse(
        status_code=502,
        content={**_PLACE_UNKNOWN_BODY, "submission_id": submission_id},
    )


def _record_place_event(submission_id: str, detail: dict) -> None:
    """Best-effort IB_NOTICE audit row; a failed write never fails the place."""
    try:
        orders_store.record_event(submission_id, "IB_NOTICE", detail)
    except Exception:  # pragma: no cover — event writes are best-effort
        logger.warning("Failed to record IB_NOTICE event for submission %s", submission_id, exc_info=True)


# F5.4 — cancel/modify failure classification
# ib_reject codes that mean "order vanished" map to 404 so the UI can
# distinguish from a hard reject. All other ib_reject codes are 400.
_IB_REJECT_NOT_FOUND_CODES = {"10147", "10148"}


def _map_ib_reject_status(upstream_code: Any) -> int:
    code = str(upstream_code) if upstream_code is not None else ""
    return 404 if code in _IB_REJECT_NOT_FOUND_CODES else 400


def _classify_to_http(data: dict) -> tuple[int, str]:
    """Map an order-action reply classification → (http_status, reason_code)."""
    classification = data.get("classification")
    if classification in ("connection", "send_window_closed", "unconfirmed"):
        # "unconfirmed": the action reached IB without an ack (amend_outcome
        # carries it); the routes treat it as uncertain, never as a reject.
        return 503, ReasonCode.IB_CONNECTION.value
    if classification == BROKER_DISCONNECTED:
        return 503, ReasonCode.IB_BROKER_DISCONNECTED.value
    if classification == ORDERS_HALTED:
        return 503, ReasonCode.ORDERS_HALTED.value
    if classification == "ownership":
        return 409, ReasonCode.ORDER_NOT_OWNED.value
    if classification == "ib_reject":
        upstream = data.get("upstream") or {}
        return _map_ib_reject_status(upstream.get("code")), ReasonCode.IB_REJECT.value
    # Unknown/missing classification — fall back to 502 with a generic code.
    return 502, ReasonCode.IB_REJECT.value


def order_error(status: int, reason_code: str, detail: dict, **extra) -> JSONResponse:
    """Single error contract for the cancel/modify routes.

    Emits ``reason_code`` at the TOP level of the body — that is what the
    web toast helpers read via ``readReasonCode`` (``HTTPException(
    detail=dict)`` only ever produced ``body.detail.reason_code``). The
    structured payload stays under ``detail`` for the readers that consume
    it (``detail.applied`` on MODIFY_STALE, ``upstream``, ``applied_
    sequence``, ``http_status``); ``detail.reason_code`` is kept too so
    nested readers keep working. ``**extra`` lands at the top level.
    """
    return JSONResponse(
        status_code=status,
        content={
            "reason_code": reason_code,
            "detail": {**detail, "reason_code": reason_code},
            **extra,
        },
    )


def _record_manage_event(
    ib_order_id: str,
    kind: str,
    detail: dict,
    perm_id: str = "",
    scope: dict[str, str] | None = None,
) -> None:
    """Write orders_events row for a cancel/modify attempt.

    Resolves the submission canonically (`load_submission_for_modify`:
    permId first, an orderId alone only when it names exactly one row —
    orderIds are unique per clientId only). If no submission exists (order placed pre-F4, or before a
    reserve_attempt row was created), the event is skipped — orders_events
    .submission_id is NOT NULL and has no synthetic parent row available.
    """
    try:
        scope = scope or _resolve_scope_kwargs()
        row = orders_store.load_submission_for_modify(order_id=ib_order_id, perm_id=perm_id, **scope)
        if row:
            orders_store.record_event(row["submission_id"], kind, detail)
    except Exception:  # pragma: no cover — event writes are best-effort
        logger.warning(
            "Failed to record %s event for order=%s perm=%s",
            kind,
            ib_order_id,
            perm_id,
            exc_info=True,
        )


@app.post("/orders/cancel")
async def orders_cancel(request: Request):
    """Cancel an open order through the single order owner (PR-10).

    IB scopes cancelOrder by clientId, so only orders placed by this API's
    ``orders`` connection are cancelled here; any other placing clientId →
    409 ORDER_NOT_OWNED (cancel it in TWS / IBKR Mobile). Reply mapping:
      classification=connection          → 503 IB_CONNECTION
      classification=broker_disconnected → 503 IB_BROKER_DISCONNECTED
      classification=orders_halted       → 503 ORDERS_HALTED
      classification=ownership           → 409 ORDER_NOT_OWNED
      classification=ib_reject           → 400 (or 404 for 10147/10148) IB_REJECT
    The full upstream payload (code + message) is preserved in detail.
    """
    if is_read_only():
        return read_only_403()
    body = await request.json()
    return await _orders_cancel_from_body(body)


async def _orders_cancel_from_body(body: dict):
    denial = ensure_order_allowed("order cancellation", app.state)
    if denial is not None:
        return denial
    _kind, response = await _cancel_order(body)
    return response


def _int_id(value: Any) -> int:
    """orderId/permId from a request body (0 when absent); ValueError if not an integer."""
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        raise ValueError(f"not an order id: {value!r}")
    return int(value)


_NOT_OWNED_HINT = "Cancel or modify it in TWS / IBKR Mobile."


def _not_owned_refusal(placing_client_id: Any, order_id: Any, **extra) -> JSONResponse | None:
    """Positive routing (A1): only an order placed by this environment's
    ``orders`` connection takes the owner path. A recorded placing clientId
    that is anything else (legacy subprocess 20–49 / 26, clientId 0 / TWS /
    mobile, relay, another environment's base) → 409 ORDER_NOT_OWNED with no
    broker work at all. No recorded clientId → the owner path, whose fresh
    scoped open-order snapshot refuses a foreign owner before any send."""
    if placing_client_id is None:
        return None
    owner = pool_client_id("orders")
    try:
        placing = int(placing_client_id)
    except (TypeError, ValueError):
        placing = None
    if placing == owner:
        return None
    return order_error(
        409,
        ReasonCode.ORDER_NOT_OWNED.value,
        {
            "classification": "ownership",
            "message": (
                f"Order {order_id} was placed by clientId {placing_client_id}, not this API's order "
                f"connection (clientId {owner}). Nothing was sent. {_NOT_OWNED_HINT}"
            ),
            "originalClientId": placing,
            "http_status": 409,
            **extra,
        },
    )


def _owner_unavailable(**extra) -> JSONResponse:
    return order_error(
        503,
        ReasonCode.IB_BROKER_DISCONNECTED.value,
        {"message": "Order owner is not running; nothing was sent", "http_status": 503, **extra},
    )


def _owner_error_detail(data: dict, **extra) -> tuple[int, str, dict]:
    """(http_status, reason_code, detail) for an error reply from the owner."""
    http_status, reason_code = _classify_to_http(data)
    message = data.get("message")
    if reason_code == ReasonCode.ORDER_NOT_OWNED.value:
        message = f"{message}. {_NOT_OWNED_HINT}"
    detail = {
        "reason_code": reason_code,
        "classification": data.get("classification"),
        "message": message,
        "upstream": data.get("upstream"),
        "amend_outcome": data.get("amend_outcome"),
        "http_status": http_status,
        **extra,
    }
    return http_status, reason_code, detail


async def _cancel_order(body: dict, row: dict | None = None, *, reason_code: str = "USER_CANCEL") -> tuple[str, Any]:
    """Cancel one order through the order owner → (kind, response). kind:
    "ok"; "failed" (nothing was sent, or IB said no and the order is still
    working); "uncertain" (the cancel may have reached IB unconfirmed).

    `row` = the target's submission row already resolved by the caller
    ({submission_id, placing_client_id}); else it is resolved canonically
    here. Only that row is marked CANCELLED (with `reason_code`)."""
    if _is_test_mode():
        return "ok", {
            "status": "ok",
            "message": "Cancel accepted in test mode",
            "echo": body,
        }

    order_id = body.get("orderId", 0)
    perm_id = body.get("permId", 0)

    if not order_id and not perm_id:
        return "failed", order_error(
            400,
            ReasonCode.ORDER_IDENTIFIER_REQUIRED.value,
            {
                "message": "Cancel request must include orderId or permId.",
                "http_status": 400,
            },
        )
    try:
        oid, pid = _int_id(order_id), _int_id(perm_id)
    except (TypeError, ValueError):
        return "failed", order_error(
            400,
            ReasonCode.ORDER_IDENTIFIER_REQUIRED.value,
            {"message": "orderId and permId must be integers.", "http_status": 400},
        )

    scope = _resolve_scope_kwargs()

    # PR-05 identity tuple: the core asserts the scope account plus the
    # recorded placing clientId (when a submission row carries one), so a
    # cancel can never land on a different account's/client's order. The
    # account is asserted only when the scope resolved a real account — the
    # legacy_unknown fallback would otherwise fail every identity check.
    _broker_account = scope.get("broker_account") or ""
    account = _broker_account if _broker_account and _broker_account != "legacy_unknown" else None
    if row is None:
        if not perm_id and (
            orders_store.count_submissions_for_order_id(str(order_id), client_id=body.get("clientId"), **scope) > 1
        ):
            # Several rows share this orderId (different clients): the core
            # could cancel the wrong one. No row at all (TWS order) → owner
            # path, which refuses an order it does not own.
            return "failed", order_error(
                409,
                ReasonCode.ORDER_TARGET_AMBIGUOUS.value,
                {
                    "message": f"orderId {order_id} names several orders; send its permId. Nothing was cancelled.",
                    "http_status": 409,
                },
            )
        row = orders_store.load_submission_for_modify(
            order_id=str(order_id or ""), perm_id=str(perm_id or ""), client_id=body.get("clientId"), **scope
        )
    placing = row.get("placing_client_id") if row else None

    refusal = _not_owned_refusal(placing, order_id or perm_id)
    if refusal is None and order_owner is None:
        refusal = _owner_unavailable()
    if refusal is not None:
        _record_manage_event(
            str(order_id or ""), "CANCEL", _json_body(refusal)["detail"], perm_id=str(perm_id or ""), scope=scope
        )
        return "failed", refusal

    client_id = int(placing) if placing is not None else None
    data = await order_owner.run(
        lambda client: ib_order_manage.cancel_order_on_client(client, oid, pid, account=account, client_id=client_id),
        timeout=_ORDER_ACTION_TIMEOUT_S,
    )
    if data.get("status") == "error":
        http_status, reason_code, detail = _owner_error_detail(data)
        _record_manage_event(str(order_id or ""), "CANCEL", detail, perm_id=str(perm_id or ""), scope=scope)
        kind = "uncertain" if data.get("amend_outcome") == "unconfirmed" else "failed"
        return kind, order_error(http_status, reason_code, detail)

    _record_manage_event(
        str(order_id or ""),
        "CANCEL",
        {"status": data.get("status"), "message": data.get("message"), "http_status": 200},
        perm_id=str(perm_id or ""),
    )
    _mark_submission_cancelled((row or {}).get("submission_id"), reason_code=reason_code)
    return "ok", data


@app.post("/orders/replace")
async def orders_replace(request: Request):
    """Combo replacement: claim → cancel the original(s) → place the successor,
    owned by ONE process (PR-07 fix3, OP-1). IB has no atomic restructure of
    a BAG's legs, so cancel+place is unavoidable; the claim makes it safe:
    a second replacement of the same target is refused before any cancel."""
    if is_read_only():
        return read_only_403()
    body = await request.json()
    return await _orders_replace_from_body(body)


def _replace_targets(body: dict) -> list[dict]:
    """Deduplicated cancel targets: body.cancelOrders, else orderId/permId
    (+ optional clientId, the placing client — needed for an orderId-only
    target when the scope holds more than one row with that orderId)."""
    raw = body.get("cancelOrders") or [
        {"orderId": body.get("orderId"), "permId": body.get("permId"), "clientId": body.get("clientId")}
    ]
    seen: set[str] = set()
    targets: list[dict] = []
    for t in raw if isinstance(raw, list) else []:
        try:
            oid = int((t or {}).get("orderId") or 0)
            perm = int((t or {}).get("permId") or 0)
            client = (t or {}).get("clientId")
            client = int(client) if client is not None else None
        except (TypeError, ValueError, AttributeError):
            continue
        if oid <= 0 and perm <= 0:
            continue
        key = f"perm:{perm}" if perm > 0 else f"oid:{client}:{oid}"
        if key in seen:
            continue
        seen.add(key)
        targets.append({"orderId": oid, "permId": perm, "clientId": client})
    return targets


def _json_body(resp: Any) -> dict:
    if isinstance(resp, JSONResponse):
        return json.loads(bytes(resp.body))
    return dict(resp or {})


_REPLACE_FAILED_COPY = "CRITICAL: Original order cancelled, replacement FAILED. Place a new order manually."
_REPLACE_UNKNOWN_COPY = (
    "Original order cancelled; replacement submitted, status unknown — check working orders. Do not resubmit."
)


async def _orders_replace_from_body(body: dict):
    denial = ensure_order_allowed("order replacement", app.state)
    if denial is not None:
        return denial
    replacement = body.get("replaceOrder")
    if not isinstance(replacement, dict):
        return JSONResponse(
            status_code=400,
            content={"detail": "replaceOrder is required", "reason_code": ReasonCode.INVALID_ORDER_BODY.value},
        )
    targets = _replace_targets(body)
    if not targets:
        return JSONResponse(
            status_code=400,
            content={
                "detail": "At least one order to cancel is required",
                "reason_code": ReasonCode.ORDER_IDENTIFIER_REQUIRED.value,
            },
        )
    prep = await _prepare_place(replacement)
    if isinstance(prep, JSONResponse):
        return prep
    cid, req_row, candidate, legs_payload, scope = prep

    # 1. Claim (one txn, scope advisory lock): refuse a second replacement
    #    of any target and any reused attempt id; else the successor's row
    #    is reserved PENDING through the Gate-4 insert path.
    outcome = gate4.claim_replacement(
        "local",
        cid,
        req_row,
        candidate=candidate,
        legs_payload=legs_payload,
        scope=scope,
        targets=[
            {"perm_id": str(t["permId"] or ""), "ib_order_id": str(t["orderId"] or ""), "client_id": t["clientId"]}
            for t in targets
        ],
    )
    refusal = _reservation_refusal(outcome, cid, replacement=True)
    if refusal is not None:
        return refusal
    submission_id = outcome.submission_id

    # 2. Cancel every target by its resolved identity (permId when known)
    #    and persist the cancel on exactly its row. Nothing is placed unless
    #    all are confirmed.
    cancelled: list[dict] = []
    for ident in outcome.targets:
        target = {"orderId": int(ident["ib_order_id"] or 0), "permId": int(ident["perm_id"] or 0)}
        row = {"submission_id": ident["submission_id"], "placing_client_id": ident["client_id"]}
        kind, resp = await _cancel_order({k: v for k, v in target.items() if v}, row=row)
        if kind == "failed":
            orders_store.mark_terminal(
                submission_id=submission_id,
                state="FAILED",
                reason_code="REPLACE_CANCEL_FAILED",
                filled_qty=0,
                avg_fill_price=None,
                expected_states=("PENDING",),
            )
            status = resp.status_code if isinstance(resp, JSONResponse) else 502
            return JSONResponse(
                status_code=status,
                content={**_json_body(resp), "replacement_placed": False, "cancelled": cancelled},
            )
        if kind == "uncertain":
            orders_store.mark_place_unknown(
                submission_id=submission_id,
                ib_order_id=None,
                perm_id=None,
                placing_client_id=None,
                reason_code="REPLACE_CANCEL_UNCERTAIN",
                expected_states=("PENDING",),
            )
            return JSONResponse(
                status_code=502,
                content={
                    **_PLACE_UNKNOWN_BODY,
                    "error": "Cancel of the original order is unconfirmed; the replacement was NOT placed. "
                    "Check working orders. Do not resubmit.",
                    "replacement_placed": False,
                    "submission_id": submission_id,
                },
            )
        cancelled.append(target)

    # 3. Gate 4 again on the post-cancel view, then the normal place path
    #    for the claimed row (never the PENDING-duplicate branch).
    if candidate is not None:
        rejected = gate4.recheck_claim(submission_id, candidate, scope)
        if rejected is not None:
            verdict = _gate4_reject_verdict(
                rejected.reasons, rejected.uncovered, rejected.short_stock, rejected.split_required
            )
            orders_store.mark_terminal(
                submission_id=submission_id,
                state="REJECTED",
                reason_code=verdict.reason_code.value if verdict.reason_code else "GATE4_REJECT",
                filled_qty=0,
                avg_fill_price=None,
                expected_states=("PENDING",),
            )
            resp = _preflight_block_response(verdict)
            return JSONResponse(
                status_code=resp.status_code, content={**_json_body(resp), "error": _REPLACE_FAILED_COPY}
            )
    resp = await _place_reserved(replacement, submission_id, scope)
    if not isinstance(resp, JSONResponse) or resp.status_code < 400:
        return resp
    content = _json_body(resp)
    unknown = content.get("reason_code") == ReasonCode.PLACE_OUTCOME_UNKNOWN.value
    return JSONResponse(
        status_code=resp.status_code,
        content={**content, "error": _REPLACE_UNKNOWN_COPY if unknown else _REPLACE_FAILED_COPY},
    )


def _mark_submission_cancelled(submission_id: str | None, *, reason_code: str = "USER_CANCEL") -> None:
    """Transition exactly this order_submissions row to CANCELLED after a
    successful cancel. The activity poller cannot do this on its own — naive
    disappearance-detection misclassifies fills as cancels — so the cancel
    path is the only authoritative trigger. The row comes from a canonical
    resolve (permId, or a unique clientId+orderId), never an orderId-first
    lookup: orderIds repeat across clients, and marking the wrong live order
    terminal would drop its Gate-4 demand. No row → nothing is marked.
    """
    if not submission_id:
        return
    try:
        # filled_qty=None keeps executed quantity: a cancel releases only
        # the unfilled remainder (spec §2.3), even before the fill row lands.
        orders_store.mark_terminal(
            submission_id=submission_id,
            state="CANCELLED",
            reason_code=reason_code,
            filled_qty=None,
            avg_fill_price=None,
            # Never over FILLED or an earlier terminal state (a fill or a
            # TWS-cancel mirror that landed first wins).
            expected_states=orders_store.NON_TERMINAL_STATES,
        )
    except Exception:  # pragma: no cover — best-effort
        logger.warning("Failed to mark submission %s CANCELLED", submission_id, exc_info=True)


@app.post("/orders/modify")
async def orders_modify(request: Request):
    """Modify an open order through the single order owner (PR-10).

    Only orders placed by this API's ``orders`` connection are modified
    (409 ORDER_NOT_OWNED otherwise). The worker settles the Gate 4 amend.

    F5.4: modifySequence monotonic gate runs BEFORE the owner is called.
      - missing modifySequence → 400 MODIFY_SEQUENCE_REQUIRED
      - unknown ib_order_id     → 404 ORDER_NOT_FOUND
      - stale sequence          → 409 MODIFY_STALE (detail.applied=<current>)
    Owner replies are classified the same as cancel.
    """
    if is_read_only():
        return read_only_403()
    body = await request.json()
    return await _orders_modify_from_body(body)


def _confirmed_int(confirmed, requested) -> int | None:
    """Broker-confirmed quantity when the CLI reports one, else the request
    (a quantity amend confirms only on exact equality)."""
    for value in (confirmed, requested):
        if value is None or isinstance(value, bool):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


async def _orders_modify_from_body(body: dict):
    denial = ensure_order_allowed("order modification", app.state)
    if denial is not None:
        return denial

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
        return order_error(
            400,
            ReasonCode.MODIFY_SEQUENCE_REQUIRED.value,
            {
                "message": "modifySequence is required",
                "http_status": 400,
            },
        )
    try:
        modify_sequence = int(modify_sequence)
    except (TypeError, ValueError):
        return order_error(
            400,
            ReasonCode.MODIFY_SEQUENCE_REQUIRED.value,
            {
                "message": "modifySequence must be an integer",
                "http_status": 400,
            },
        )

    # Apply sequence gate BEFORE the owner is called. If only permId is
    # supplied (UI-initiated modifies often have orderId=0), route through the
    # perm_id variant so we can still find the submissions row.
    if not order_id and not perm_id:
        return order_error(
            400,
            ReasonCode.ORDER_IDENTIFIER_REQUIRED.value,
            {
                "message": "Modify request must include orderId or permId.",
                "http_status": 400,
            },
        )
    _scope = _resolve_scope_kwargs()

    if not order_id and perm_id:
        seq_outcome = orders_store.apply_modify_by_perm_id(str(perm_id), modify_sequence, **_scope)
    else:
        seq_outcome = orders_store.apply_modify(str(order_id), modify_sequence, **_scope)
    if not seq_outcome["applied"]:
        current = seq_outcome["current_sequence"]
        if current == -1:
            return order_error(
                404,
                ReasonCode.ORDER_NOT_FOUND.value,
                {
                    "message": f"Order {order_id} not found",
                    "http_status": 404,
                },
            )
        return order_error(
            409,
            ReasonCode.MODIFY_STALE.value,
            {
                "message": f"modifySequence {modify_sequence} is stale; current is {current}",
                "applied": current,
                "http_status": 409,
            },
        )

    try:
        oid, pid = _int_id(order_id), _int_id(perm_id)
        price = float(new_price) if new_price is not None else None
        quantity = int(new_quantity) if new_quantity is not None else None
    except (TypeError, ValueError):
        return order_error(
            400,
            ReasonCode.INVALID_ORDER_BODY.value,
            {
                "message": "orderId/permId/newQuantity must be integers and newPrice a number",
                "applied_sequence": modify_sequence,
                "http_status": 400,
            },
        )

    # PR-05 identity tuple: the core asserts the scope account plus the
    # recorded placing clientId (when a submission row carries one), so a
    # modify can never land on a different account's/client's order. The
    # account is asserted only when the scope resolved a real account.
    _broker_account = _scope.get("broker_account") or ""
    account = _broker_account if _broker_account and _broker_account != "legacy_unknown" else None
    _row = orders_store.load_submission_for_modify(
        order_id=str(order_id or ""), perm_id=str(perm_id or ""), **_scope
    )
    placing = _row.get("placing_client_id") if _row else None

    # Positive routing (A1) before any amend opens: an order this API's
    # orders connection does not own is refused with no broker work.
    refusal = _not_owned_refusal(placing, order_id or perm_id, applied_sequence=modify_sequence)
    if refusal is None and order_owner is None:
        refusal = _owner_unavailable(applied_sequence=modify_sequence)
    if refusal is not None:
        _record_manage_event(
            str(order_id or ""), "MODIFY", _json_body(refusal)["detail"], perm_id=str(perm_id or ""), scope=_scope
        )
        return refusal

    # BAG guarantee mode is durable order state, never client-mutable. The
    # broker always receives it explicitly — a resubmitted BAG without
    # NonGuaranteed raises "Missing or invalid NonGuaranteed value", and a
    # default could silently flip a guaranteed combo to non-guaranteed.
    # Checked BEFORE the amend opens so a refused mode change never leaves
    # a pending amend behind.
    effective_ng: int | None = None
    if _row and _row.get("security_type") == "BAG":
        stored_guaranteed = gate4.persisted_guarantee(_row)
        effective_ng = 0 if stored_guaranteed else 1
        requested_ng = body.get("nonGuaranteed")
        if requested_ng is not None:
            try:
                req_ng = int(requested_ng)
            except (TypeError, ValueError):
                req_ng = -1
            if req_ng != effective_ng:
                return order_error(
                    400,
                    ReasonCode.MODIFY_GUARANTEE_MODE.value,
                    {
                        "message": "Guarantee mode is fixed at placement; cannot change NonGuaranteed on modify",
                        "http_status": 400,
                    },
                )

    # Gate 4 (PR-03b): every modify of a known row opens one amend generation
    # under the account-scope advisory lock. A row with an unresolved amend
    # refuses the new modify (MODIFY_PENDING). A quantity modify evaluates
    # the same coverage model as placement and persists pending_amend_qty, so
    # an acknowledgement lost in flight keeps reserving max(old, new) - filled.
    _submission_id = _row.get("submission_id") if _row else None
    _amend_id: str | None = None
    _send_by = None
    if new_quantity is not None and _submission_id is None:
        return order_error(
            404,
            ReasonCode.ORDER_NOT_FOUND.value,
            {
                "message": "Quantity modify requires a resolvable submission row",
                "http_status": 404,
            },
        )
    if _submission_id is not None:
        mod_outcome = gate4.begin_modify(_submission_id, new_quantity, _scope)
        if not mod_outcome.accept:
            if gate4.MODIFY_PENDING_REASON in mod_outcome.reasons:
                return order_error(
                    409,
                    ReasonCode.MODIFY_PENDING.value,
                    {
                        "message": (
                            "An earlier change to this order is not confirmed by IB yet. "
                            "Retry in about two minutes, after IB reports the order again, or cancel the order."
                        ),
                        "http_status": 409,
                    },
                )
            v = _gate4_reject_verdict(
                mod_outcome.reasons, mod_outcome.uncovered, mod_outcome.short_stock, mod_outcome.split_required
            )
            return order_error(
                400,
                v.reason_code.value if v.reason_code else ReasonCode.GATE4_UNRESOLVED.value,
                {
                    "message": v.reason_detail or "Modify blocked by coverage gate",
                    "http_status": 400,
                },
            )
        _amend_id = mod_outcome.amend_id
        # Send-by fence (round 5, item 2): the core re-checks it right
        # before placeOrder, so a job that starts late sends nothing.
        _send_by = mod_outcome.send_by

    def _settle_amend(reply: dict) -> None:
        """Gate 4 settlement with the captured amend id — runs exactly once,
        on the worker for a started job (even after this request timed out),
        never "uncertain" from the request path for a job the worker owns.
        Only an amend proven unapplied (never sent, or an explicit IB
        rejection) releases the reservation; anything else is uncertain."""
        if _amend_id is None:
            return
        if reply.get("status") == "ok":
            gate4.settle_modify(
                _submission_id,
                _amend_id,
                "confirmed",
                _scope,
                # The broker's confirmed values, never the request:
                # confirmation tolerates a <0.001 price difference.
                new_qty=_confirmed_int(reply.get("confirmedQuantity"), new_quantity),
                new_limit_price=reply.get("confirmedPrice"),
            )
            return
        outcome = "rejected" if reply.get("amend_outcome") in ("rejected", "not_sent") else "uncertain"
        gate4.settle_modify(_submission_id, _amend_id, outcome, _scope)

    client_id = int(placing) if placing is not None else None
    data = await order_owner.run(
        lambda client: ib_order_manage.modify_order_on_client(
            client,
            oid,
            pid,
            price,
            outside_rth=outside_rth if isinstance(outside_rth, bool) else None,
            new_quantity=quantity,
            account=account,
            client_id=client_id,
            non_guaranteed=effective_ng,
            send_by=_send_by,
        ),
        timeout=_ORDER_ACTION_TIMEOUT_S,
        settle=_settle_amend,
    )

    if data.get("status") == "error":
        http_status, reason_code, detail = _owner_error_detail(data, applied_sequence=modify_sequence)
        _record_manage_event(str(order_id or ""), "MODIFY", detail, perm_id=str(perm_id or ""), scope=_scope)
        return order_error(http_status, reason_code, detail)

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
    - **Off-loop blocking calls.** The Futu SDK is fully sync; the call runs
      on a dedicated single-worker executor. While a worker is still alive
      (e.g. after a timeout), new requests 503 instead of queueing behind the
      client's RLock; a timeout also resets the client via ``disconnect()``.
    """
    global _futu_last_sync_monotonic, _futu_sync_future
    import time as _time

    lock = _get_futu_lock()

    # Early cooldown check — avoids lock contention when cache is fresh.
    if _futu_in_cooldown():
        cached = _read_cache(DATA_DIR / "futu_portfolio.json")
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
            cached = _read_cache(DATA_DIR / "futu_portfolio.json")
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
async def blotter_sync(scope=Depends(get_broker_scope)):
    """Return historical trades from Postgres first, then optional IB Flex.

    When IB_FLEX_TOKEN / IB_FLEX_QUERY_ID are unset, return a structured
    empty payload with configured=False rather than a 502, so the UI can
    show a friendly empty state. The Flex CLI emits exit code 2 + a
    JSON marker `{"error":"FLEX_NOT_CONFIGURED",...}` for that case
    (see src/xenon/trade_blotter/flex_query.py). Plan: docs/plans/
    2026-04-28-postgres-migration-completion-IMPL.md § W2.1.
    """
    if scope.broker == "FUTU":
        # FUTU: best-effort rebuild closed trades from fills, then read them back
        # (no IB Flex). Bounded connect so a down OpenD can't wedge the loop.
        if not _is_test_mode():
            await _futu_refresh_db_first(scope)
        engine = get_sync_engine()
        with engine.connect() as conn:
            return fetch_futu_blotter(conn, scope=scope, days=30)

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
async def blotter_get(scope=Depends(get_broker_scope)):
    engine = get_sync_engine()
    with engine.connect() as conn:
        if scope.broker == "FUTU":
            return fetch_futu_blotter(conn, scope=scope, days=30)
        return fetch_blotter_pg(conn, scope=scope, days=30)


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


@app.get("/market-depth")
async def market_depth(
    symbol: str,
    expiry: Optional[str] = None,
    strike: Optional[float] = None,
    right: Optional[str] = None,
    num_rows: int = Query(10, ge=1, le=20),
):
    """Point-in-time L2 order-book snapshot (subprocess; mirrors /options/chain).

    `symbol` alone fetches stock/index depth; pass the full option triplet
    (`expiry` + `strike` + `right`) for option depth. Returns the qualified
    `conId`. A no-entitlement contract is a 200 with ``entitled: false`` — not
    an error. ``entitled`` reflects the permission axis only; an empty book with
    no permission failure stays ``entitled: true`` with a ``note``.
    """
    # Normalize blank query strings to absent: an empty option field (e.g.
    # ?right=) is "omitted", not "present-but-invalid". Keeps the all-or-none
    # guard and the arg builder on the same presence predicate, so a blank field
    # yields the documented 422 instead of leaking through to a subprocess 502.
    expiry = (expiry or "").strip() or None
    right = (right or "").strip() or None

    # All-or-none option tuple — never silently degrade an option request to
    # stock depth on a partial tuple.
    opt = [expiry, strike, right]
    if any(v is not None for v in opt) and not all(v is not None for v in opt):
        raise HTTPException(status_code=422, detail="provide all of expiry/strike/right, or none")

    args = ["--symbol", symbol.upper(), "--port", str(DEFAULT_GATEWAY_PORT), "--num-rows", str(num_rows)]
    if expiry:
        args += ["--expiry", expiry]
    if strike is not None:
        args += ["--strike", str(strike)]
    if right:
        args += ["--right", right.upper()]
    result = await _run_ib_script_with_recovery("xenon-ib-market-depth", args, timeout=15)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    if result.data and result.data.get("error"):
        raise HTTPException(status_code=502, detail=result.data["error"])
    return result.data


@app.get("/options/greeks")
async def options_greeks(
    symbol: str,
    expiry: str,
    strike: float,
    right: str,
):
    """Broker-computed option greeks (IB modelGreeks) for one contract (subprocess).

    Greeks are option-only — the full triplet (``expiry`` + ``strike`` + ``right``)
    is required; FastAPI 422s a missing leg before any subprocess spawns. Returns
    the qualified ``conId``, bid/ask, and greeks (impliedVol/delta/gamma/vega/
    theta/undPrice). When IB delivers no greeks (illiquid contract, market closed)
    ``greeks`` is null with a ``note`` — still a 200, not an error.
    """
    expiry = (expiry or "").strip()
    right = (right or "").strip().upper()
    if not expiry:
        raise HTTPException(status_code=422, detail="expiry is required (YYYYMMDD)")
    if right not in ("C", "P"):
        raise HTTPException(status_code=422, detail="right must be C or P")

    args = [
        "--symbol",
        symbol.upper(),
        "--expiry",
        expiry,
        "--strike",
        str(strike),
        "--right",
        right,
        "--port",
        str(DEFAULT_GATEWAY_PORT),
    ]
    result = await _run_ib_script_with_recovery("xenon-ib-option-greeks", args, timeout=20)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    if result.data and result.data.get("error"):
        raise HTTPException(status_code=502, detail=result.data["error"])
    return result.data


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

# Cooldown: after an IB data subprocess fails with a connection error, skip
# subsequent attempts for this many seconds to avoid churn. Order writes
# never spawn (PR-10: they run on the order owner).
_IB_SCRIPT_COOLDOWN_SECS = 15.0
_ib_last_failure: dict[str, float] = {"data": 0.0}
# The order owner's wait for one place: readiness round trip + qualify + ack
# wait (5 s combo) + queueing behind another order action.
_PLACE_TIMEOUT_S = 30
# Lifespan shutdown budget. uvicorn drains requests for 10 s
# (--timeout-graceful-shutdown, docker/api.Dockerfile), then the lifespan
# runs: IB disconnect wait 35 s + the other steps (sum 10 s) = 55 s, under
# the 60 s compose stop_grace_period / WATCHTOWER_TIMEOUT. The IB wait
# bounds only the wait: a disconnect queued behind a started order core is
# never dropped and runs when the core ends. A core blocked on a broker call
# with no timeout of its own (_PLACE_TIMEOUT_S bounds the HTTP caller, not
# the core) can outlive the whole budget; SIGKILL is then the backstop and
# the socket may stay open on the gateway (runbook: stale-socket check).
_SHUTDOWN_IB_DISCONNECT_S = 35
_SHUTDOWN_STEP_S = {
    "owner": 2,
    "listener": 2,
    "poller": 1,
    "wizard": 1,
    "futu_loop": 1,
    "futu": 1,
    "engine": 2,
}


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
    """Run an IB-dependent xenon-* DATA entry point with pre-flight health
    check and cooldown. Order writes never come here (order owner).

    Three layers of fast-fail:
    1. Cooldown: if a recent IB script failed, skip for _IB_SCRIPT_COOLDOWN_SECS
    2. Pool check: if pool is disconnected, verify Gateway before spawning
    3. Post-failure: verify Gateway health before restarting
    """

    # Layer 1: Cooldown — skip if a recent failure occurred
    now = time.monotonic()
    last_failure = _ib_last_failure["data"]
    if last_failure > 0 and (now - last_failure) < _IB_SCRIPT_COOLDOWN_SECS:
        elapsed = now - last_failure
        logger.debug(
            "Skipping %s — IB cooldown active (%.1fs since last failure, %ds cooldown)",
            entry,
            elapsed,
            _IB_SCRIPT_COOLDOWN_SECS,
        )
        return ScriptResult(
            ok=False,
            error="IB Gateway connection recently failed. Retrying shortly.",
            spawned=False,
        )

    # Layer 2: Pre-flight pool check
    if not _pool_has_any_connection():
        gw_status = await check_ib_gateway()
        port_ok = gw_status.get("port_listening", False)
        upstream_dead = gw_status.get("upstream_dead", False)

        if not port_ok or upstream_dead:
            _ib_last_failure["data"] = now
            logger.warning(
                "Skipping %s — Gateway down (port=%s, upstream_dead=%s), pool disconnected",
                entry,
                port_ok,
                upstream_dead,
            )
            return ScriptResult(
                ok=False,
                error="IB Gateway is not accepting connections. Check IBKR Mobile for 2FA approval.",
                spawned=False,
            )

    result = await run_entry_point(entry, args, timeout=timeout)

    # Clear cooldown on success
    if result.ok:
        _ib_last_failure["data"] = 0.0

    if not result.ok and _is_ib_connection_error(result.error):
        # Set cooldown to prevent churn from repeated failures
        _ib_last_failure["data"] = time.monotonic()

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
            result = ScriptResult(ok=False, error=msg, spawned=result.spawned)
        else:
            logger.warning(
                "IB Gateway unreachable (port=%s, upstream_dead=%s), attempting auto-restart...",
                port_ok,
                upstream_dead,
            )
            gw_result = await restart_ib_gateway()

            if gw_result.get("restarted") and gw_result.get("port_listening"):
                _ib_last_failure["data"] = 0.0  # Clear cooldown after successful restart
                if ib_pool:
                    # orders is left to the order owner's supervisor (A2):
                    # the restart is noted so its socket loss is an outage.
                    if order_owner is not None:
                        order_owner.note_gateway_restart()
                    await ib_pool.disconnect_all(roles=("sync", "data"))
                    await ib_pool.connect_all(roles=("sync", "data"))
                logger.info("IB Gateway restarted, retrying %s", entry)
                result = await run_entry_point(entry, args, timeout=timeout)
            else:
                logger.error("IB Gateway restart failed: %s", gw_result)
                result = ScriptResult(
                    ok=False,
                    error=f"IB Gateway is down and restart failed. {gw_result.get('error', '')}".strip()
                    + " Check IBKR Mobile for 2FA approval.",
                    spawned=result.spawned,
                )

    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse

    import uvicorn

    argparse.ArgumentParser(
        prog="xenon-api", description="Run the FastAPI bridge on 127.0.0.1:8321 with reload (dev)."
    ).parse_args()
    uvicorn.run(
        "xenon.api.server:app",
        host="127.0.0.1",
        port=8321,
        reload=True,
        reload_dirs=[str(SRC_DIR)],
    )


if __name__ == "__main__":
    main()
