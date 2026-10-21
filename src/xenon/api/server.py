"""Xenon FastAPI server — replaces Python shell-outs from Next.js.

Persistent IB connections, shared UW client, uniform JSON responses.
Port 8321, no auth for local use.

Usage:
    python3 -m uvicorn xenon.api.server:app --host 127.0.0.1 --port 8321 --reload
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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
    is_read_only,
    read_only_403,
)
from xenon.api.ib_gateway import check_ib_gateway, ensure_ib_gateway, is_cloud_mode, is_docker_mode, restart_ib_gateway
from xenon.api.ib_pool import IBPool, check_client_id_base_mapping
from xenon.utils.ib_connection import TEST_BROKER_GUARD_ENV
from xenon.api.order_owner import OrderOwner
from xenon.api.routes.historical import router as historical_router
from xenon.api.routes.journal import router as journal_router
from xenon.api.routes.orders import router as orders_router
from xenon.api.routes.performance import router as performance_router
from xenon.api.routes.trades import router as trades_router
from xenon.api.routes.watchlist import router as watchlist_router
from xenon.api.routes.wizard import router as wizard_router
from xenon.api.subprocess import ScriptResult, run_entry_point
from xenon.api.routes.market import router as market_router
from xenon.api.routes.order_entry import router as order_entry_router
from xenon.api.routes import futu as futu_routes
from xenon.api.routes.futu import router as futu_router
from xenon.api.routes.blotter import router as blotter_router
from xenon.api.routes.system import router as system_router
from xenon.api.routes.portfolio import router as portfolio_router
from xenon.db.engine import dispose_engine, init_engine
from xenon.execution import orders_store
from xenon.execution.account_scope import IBAccountSelectionError, select_ib_account

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
        client = futu_routes._get_futu_client()
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
            if futu_routes._futu_client is not None:
                await _bounded_cleanup(
                    "FutuClient disconnect", asyncio.to_thread(futu_routes._futu_client.disconnect), _SHUTDOWN_STEP_S["futu"]
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


app.include_router(system_router)


app.include_router(portfolio_router)


app.include_router(order_entry_router)


app.include_router(futu_router)


app.include_router(blotter_router)


app.include_router(market_router)


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
