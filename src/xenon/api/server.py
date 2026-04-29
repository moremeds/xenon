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
from xenon.api.guards import get_account_scope, mask_account, require_mode_verified
from xenon.api.ib_gateway import check_ib_gateway, ensure_ib_gateway, is_cloud_mode, is_docker_mode, restart_ib_gateway
from xenon.api.ib_pool import IBPool
from xenon.api.pool_order_manage import pool_cancel_order, pool_modify_order
from xenon.api.routes.historical import router as historical_router
from xenon.api.routes.journal import router as journal_router
from xenon.api.routes.orders import orders_payload_for_scope
from xenon.api.routes.orders import router as orders_router
from xenon.api.routes.trades import router as trades_router
from xenon.api.routes.uw_analyze import router as uw_analyze_router
from xenon.api.routes.uw_stats import router as uw_stats_router
from xenon.api.routes.wizard import router as wizard_router
from xenon.api.subprocess import ScriptResult, run_entry_point, run_module
from xenon.api.ws_ticket import create_ticket, validate_ticket
from xenon.clients.ib_client import DEFAULT_GATEWAY_PORT
from xenon.db.engine import dispose_engine, get_sync_engine, init_engine
from xenon.db.queries.blotter import blotter_has_trades, fetch_blotter_pg
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

# Suppress verbose ib_insync logging (positions, orders at INFO level)
logging.getLogger("ib_insync").setLevel(logging.WARNING)
logging.getLogger("ib_insync.wrapper").setLevel(logging.WARNING)
logging.getLogger("ib_insync.client").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
from ib_insync import Contract, Index, Option, Stock

from xenon.clients.futu_client import FutuClient
from xenon.clients.futu_exceptions import FutuConnectionError, FutuError
from xenon.clients.uw_client import UWAPIError, UWClient, UWNotFoundError

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
        client = ib_pool.get("sync")
        if client is None:
            raise RuntimeError("ib_pool sync role has no client")
        return client

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

    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                _rehydrate_mod.rehydrate_on_boot,
                ib_client_factory=_ib_client_factory,
                orders_store=_orders_store_mod,
                **_scope_kwargs,
            ),
            timeout=10.0,
        )
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
            asyncio.to_thread(
                _combo_rehydrate_mod.rehydrate_combo_sessions,
                ib_client_factory=_ib_client_factory,
                db_path=db_path,
                **_scope_kwargs,
            ),
            timeout=10.0,
        )
        logger.info("combo wizard rehydrate completed on boot")
    except asyncio.TimeoutError:
        logger.warning("combo wizard rehydrate timed out after 10s; continuing to serve")
    except Exception as exc:  # noqa: BLE001
        logger.warning("combo wizard rehydrate failed on boot; continuing to serve: %s", exc)


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

    # Eager-load the uw_analyze cache so the most recent snapshots are in
    # memory before the first request. Without this, the first GET after
    # a restart pays the disk-read + parse cost (~2 MB JSON). The cache is
    # lazy-loaded by construction; all_entries() triggers _ensure_loaded()
    # exactly once and is a no-op on subsequent calls. Suppressed in test
    # mode so unit tests seeing an empty singleton aren't polluted by the
    # real data/uw_analyze_cache.json on disk.
    try:
        from xenon.api.routes.uw_analyze import get_portfolio_cache as _get_portfolio_cache
        from xenon.api.services import uw_analyze_cache as _uw_cache_mod

        _cache_singleton = _get_portfolio_cache()
        _preloaded = _cache_singleton.all_entries()
        _cache_path = _uw_cache_mod._DEFAULT_CACHE_PATH
        _disk_exists = _cache_path.exists()
        _disk_size = _cache_path.stat().st_size if _disk_exists else 0
        if _disk_exists and _disk_size > 64 and not _preloaded:
            # Loud: the file has real content but we hydrated nothing.
            # `_ensure_loaded` already logged the specific reason at
            # WARNING; escalate to ERROR here so ops sees it.
            logger.error(
                "uw_analyze_cache preload: disk file %s has %d bytes but in-memory "
                "cache is empty — investigate _ensure_loaded warning above",
                _cache_path,
                _disk_size,
            )
        else:
            logger.info(
                "uw_analyze_cache preloaded: %d entries (disk=%s, %d bytes)",
                len(_preloaded),
                _disk_exists,
                _disk_size,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("uw_analyze_cache preload failed: %s", exc)

    # UW Analyze daily job (15:50 ET) — runs OI snapshot + advances open
    # unusual-flow events. Background asyncio task; cancelled on shutdown.
    #
    # Multi-worker guard: only worker 0 runs the daily job. Under
    # `uvicorn --workers N`, every worker would otherwise spin up its own
    # cron loop and fire N× at 15:50 ET. Uvicorn doesn't set a per-worker
    # env var by default, so we check XENON_DAILY_JOB_WORKER_ID which
    # operators can set per-worker (default "0" runs; any other value
    # suppresses). Fall-through for single-worker deployments (no env var).
    uw_daily_task = None
    _daily_worker_id = os.environ.get("XENON_DAILY_JOB_WORKER_ID", "0")
    if _daily_worker_id != "0":
        logger.info(
            "uw_analyze_daily_job suppressed on worker_id=%s (multi-worker guard)",
            _daily_worker_id,
        )
    else:
        try:
            from xenon.api.routes.uw_analyze import get_flow_log, get_portfolio_cache
            from xenon.api.services.uw_analyze_daily_job import run_loop as uw_daily_run_loop
            from xenon.clients.uw_client import UWClient

            _uw_client = UWClient() if uw_available else None

            async def _default_contract_fetcher(*, ticker: str, side: str, strike: float, expiry: str):
                """Resolve a single OCC contract's current oi/mid/underlying/volume.

                Fetches the full chain for the expiry, finds the matching strike+side row.
                Returns None on any failure so the caller falls back to expiry-only closeout.
                """
                if _uw_client is None:
                    return None
                try:
                    resp = await asyncio.to_thread(_uw_client.get_option_chain, ticker, expiry=expiry)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("default contract_fetcher chain fetch failed for %s: %s", ticker, exc)
                    return None
                rows = resp.get("data") if isinstance(resp, dict) else None
                if not isinstance(rows, list):
                    return None
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    try:
                        if abs(float(r.get("strike", -1)) - float(strike)) > 1e-6:
                            continue
                    except (TypeError, ValueError):
                        continue
                    key = "call" if side == "call" else "put"
                    try:
                        return {
                            "oi": int(float(r.get(f"{key}_oi") or 0)),
                            "mid": float(r.get(f"{key}_mid") or r.get(f"{key}_last") or 0.0),
                            "underlying_price": float(r.get("underlying_price") or 0.0),
                            "volume": int(float(r.get(f"{key}_volume") or 0)),
                        }
                    except (TypeError, ValueError):
                        return None
                return None

            uw_daily_task = asyncio.create_task(
                uw_daily_run_loop(
                    cache=get_portfolio_cache(),
                    flow_log=get_flow_log(),
                    uw_client=_uw_client,
                    contract_fetcher=_default_contract_fetcher,
                )
            )
            logger.info("uw_analyze_daily_job background task started")
        except Exception as exc:  # noqa: BLE001
            logger.warning("uw_analyze_daily_job failed to start: %s", exc)

    # F7.2 — single-leg three-source rehydrate. Runs synchronously before
    # the server starts serving so our view of in-flight orders is accurate
    # on first request. Failures are logged + swallowed so boot cannot be
    # blocked by a transient IB hiccup. Known limitation: positions_changed
    # heuristic has no persisted baseline on boot, so unknowns map to
    # UNKNOWN rather than auto-CANCELLED (per F7.1 design).
    await _run_rehydrate_on_boot()

    try:
        yield
    finally:
        # Shutdown — always runs, even if the app raised.
        if uw_daily_task is not None:
            uw_daily_task.cancel()
            try:
                await uw_daily_task
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
        # Clear long-lived in-memory singletons so `uvicorn --reload` doesn't
        # double-allocate them on module reimport. Also releases cached report
        # dicts that would otherwise pin ~100s of MB across a reload cycle.
        try:
            from xenon.api.routes import uw_analyze as _uw_route_mod

            # Null the singletons so `get_portfolio_cache()` creates a fresh
            # instance on the next reload cycle. The previous approach
            # (_entries.clear()) left `_loaded=True` on the surviving object,
            # which made `_ensure_loaded()` skip the disk read — every ticker
            # re-scanned from scratch after --reload.
            _uw_route_mod._portfolio_cache = None
            _uw_route_mod._flow_log = None
            # Recreate the OI semaphore so it binds to the new event loop
            # on the next reload cycle (asyncio.Semaphore captures the loop
            # on first acquire).
            _uw_route_mod._ON_DEMAND_OI_SEM = asyncio.Semaphore(3)
            # Close the shared UWClient if one was constructed. Leaves
            # the underlying requests.Session idle connections to be
            # released rather than lingering across reload cycles.
            _shared_client = _uw_route_mod._uw_client_singleton
            if _shared_client is not None:
                try:
                    _close = getattr(_shared_client, "close", None)
                    if callable(_close):
                        _close()
                except Exception as close_exc:  # noqa: BLE001
                    logger.debug("shared UWClient close failed: %s", close_exc)
                _uw_route_mod._uw_client_singleton = None
        except Exception as exc:  # noqa: BLE001
            logger.warning("uw_analyze singleton clear on shutdown failed: %s", exc)
        # Persist UW API stats hourly history so daily stats survive a
        # FastAPI restart. Best-effort — never block shutdown on I/O.
        try:
            from xenon.utils.uw_api_stats import stats as _uw_stats

            _uw_stats.flush_history()
        except Exception as exc:  # noqa: BLE001
            logger.warning("uw_api_stats history flush on shutdown failed: %s", exc)
        await dispose_engine()
        logger.info("Xenon API shut down")


app = FastAPI(title="Xenon API", version="1.0.0", lifespan=lifespan)
app.include_router(historical_router)
app.include_router(journal_router)
app.include_router(orders_router)
app.include_router(trades_router)
app.include_router(uw_analyze_router)
app.include_router(uw_stats_router)
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


def _write_scan_to_postgres(filename: str, data: dict) -> None:
    """Write a scanner result to Postgres scan_results table (best-effort).

    For 'vcg.json', also writes a row to xenon.vcg_series so generated columns
    populate. (gex.json writes its own gex_snapshots row inside gex.py.)
    """
    scan_type = _SCAN_TYPE_MAP.get(filename)
    if not scan_type:
        return
    try:
        url = os.environ.get("DATABASE_URL")
        if not url:
            return
        from sqlalchemy import create_engine as _cse
        from sqlalchemy import insert

        from xenon.db.schema import scan_results

        sync_url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
        engine = _cse(sync_url)
        with engine.begin() as conn:
            conn.execute(insert(scan_results).values(scan_type=scan_type, payload=data))
            if filename == "vcg.json":
                from xenon.db.queries.scans import save_vcg_scan

                save_vcg_scan(
                    conn,
                    payload=data,
                    market_open=data.get("market_open"),
                    credit_proxy=data.get("credit_proxy"),
                )
        engine.dispose()
    except Exception:
        logger.warning("scan archive to Postgres failed for %s", filename, exc_info=True)


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


def _normalize_risk_reversal_series(raw: object) -> List[dict]:
    """Normalize UW historical risk reversal payloads into a stable list."""
    rows: Iterable[object] = []
    if isinstance(raw, dict):
        raw_rows = raw.get("data")
        if isinstance(raw_rows, list):
            rows = raw_rows
    elif isinstance(raw, list):
        rows = raw

    normalized: List[dict] = []
    seen_dates: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = row.get("date")
        value = row.get("risk_reversal")
        if not isinstance(date, str):
            continue
        numeric = _coerce_float(value)
        if numeric is None:
            continue
        # Skip invalid or duplicate dates; keep the latest row for a date.
        if date in seen_dates:
            continue
        seen_dates.add(date)
        normalized.append({"date": date, "value": numeric})

    normalized.sort(key=lambda item: item["date"])
    return normalized


def _extract_expiry_candidates(raw: object) -> List[str]:
    rows: Iterable[object] = []
    if isinstance(raw, dict):
        raw_rows = raw.get("data")
        if isinstance(raw_rows, list):
            rows = raw_rows
    elif isinstance(raw, list):
        rows = raw

    candidates: List[str] = []
    for row in rows:
        if isinstance(row, dict):
            expiry = row.get("expiry")
            if not isinstance(expiry, str):
                expiry = row.get("expires")
            if not isinstance(expiry, str):
                expiry = row.get("expiration")
            if isinstance(expiry, str) and expiry not in candidates:
                candidates.append(expiry)
    return candidates


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


def _normalize_expiry_string(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None

    parsed = _coerce_date(value)
    if parsed is not None:
        return parsed.date().isoformat()

    compact = value.strip()
    if len(compact) == 8 and compact.isdigit():
        try:
            return datetime.strptime(compact, "%Y%m%d").date().isoformat()
        except ValueError:
            return None

    return None


def _sort_expiry_candidates(expiries: Iterable[str], now: Optional[datetime] = None) -> List[str]:
    parsed: List[Tuple[str, datetime]] = []
    seen: set[str] = set()
    for expiry in expiries:
        normalized = _normalize_expiry_string(expiry)
        if normalized is None or normalized in seen:
            continue
        parsed_date = _coerce_date(normalized)
        if parsed_date is None:
            continue
        seen.add(normalized)
        parsed.append((normalized, parsed_date))

    if not parsed:
        return []

    current = now or datetime.now(timezone.utc)
    future = sorted(
        (item for item in parsed if item[1].date() >= current.date()),
        key=lambda item: item[1],
    )
    past = sorted(
        (item for item in parsed if item[1].date() < current.date()),
        key=lambda item: item[1],
        reverse=True,
    )
    return [expiry for expiry, _ in [*future, *past]]


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
                chains = await asyncio.to_thread(
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


def _prepend_expiry(candidates: List[str], expiry: Optional[str]) -> List[str]:
    normalized = _normalize_expiry_string(expiry)
    if normalized is None:
        return candidates
    return [normalized, *[candidate for candidate in candidates if candidate != normalized]]


def _limit_expiry_candidates(candidates: List[str], max_expiries: int) -> List[str]:
    if max_expiries <= 0 or len(candidates) <= max_expiries:
        return candidates
    if max_expiries == 1:
        return candidates[:1]

    last_index = len(candidates) - 1
    selected_indices = {0, last_index}
    for slot in range(1, max_expiries - 1):
        index = round(slot * last_index / (max_expiries - 1))
        selected_indices.add(index)

    return [candidates[index] for index in sorted(selected_indices)[:max_expiries]]


def _build_internals_skew_cache_path(
    nq_ticker: str,
    spx_ticker: str,
    timeframe: str,
    nq_delta: int,
    spx_delta: int,
    nq_expiry: Optional[str],
    spx_expiry: Optional[str],
) -> Path:
    key = (
        f"v7-uw-skew-history|{nq_ticker}|{spx_ticker}|{timeframe}|"
        f"{nq_delta}|{spx_delta}|{nq_expiry or ''}|{spx_expiry or ''}"
    )
    key_hash = hashlib.md5(key.encode()).hexdigest()[:16]
    return INTERNALS_SKEW_CACHE_DIR / f"internals_skew_history_{key_hash}.json"


def _read_internals_skew_cache(path: Path) -> Optional[dict]:
    cached = _read_cache(path)
    if not isinstance(cached, dict):
        return None

    generated_at = cached.get("generated_at")
    if not isinstance(generated_at, str):
        return None

    parsed = _coerce_date(generated_at)
    if parsed is None:
        return None

    age_seconds = (datetime.now(timezone.utc) - parsed.replace(tzinfo=timezone.utc)).total_seconds()
    if age_seconds > INTERNALS_SKEW_CACHE_TTL_SECONDS:
        return None
    return cached


def _internals_skew_cache_payload(
    nq_ticker: str,
    spx_ticker: str,
    timeframe: str,
    nq_delta: int,
    spx_delta: int,
    nq_expiry: Optional[str],
    spx_expiry: Optional[str],
    nq_rows: List[dict],
    spx_rows: List[dict],
    used_nq_expiries: List[str],
    used_spx_expiries: List[str],
) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "expiry_discovery": "Unusual Whales",
            "skew_history": "Unusual Whales",
        },
        "nq": {
            "ticker": nq_ticker.upper(),
            "expiry": used_nq_expiries[0] if used_nq_expiries else None,
            "expiries": used_nq_expiries,
            "delta": nq_delta,
            "timeframe": timeframe,
            "data": nq_rows,
        },
        "spx": {
            "ticker": spx_ticker.upper(),
            "expiry": used_spx_expiries[0] if used_spx_expiries else None,
            "expiries": used_spx_expiries,
            "delta": spx_delta,
            "timeframe": timeframe,
            "data": spx_rows,
        },
    }


def _merge_risk_reversal_series(series_rows: Iterable[List[dict]]) -> List[dict]:
    merged: dict[str, float] = {}
    for rows in series_rows:
        for row in rows:
            date = row.get("date")
            value = row.get("value")
            if not isinstance(date, str) or not isinstance(value, (int, float)):
                continue
            if date not in merged:
                merged[date] = float(value)
    return [{"date": date, "value": merged[date]} for date in sorted(merged)]


def _series_span_days(rows: List[dict]) -> int:
    if len(rows) < 2:
        return 0
    start = _coerce_date(rows[0].get("date"))
    end = _coerce_date(rows[-1].get("date"))
    if start is None or end is None:
        return 0
    return (end.date() - start.date()).days


def _needs_deeper_backfill(rows: List[dict], timeframe: str) -> bool:
    if not rows:
        return True
    span_days = _series_span_days(rows)
    normalized = timeframe.upper().strip()
    if normalized in {"5Y", "ALL"}:
        return span_days < 700
    if normalized == "2Y":
        return span_days < 400
    return False


async def _resolve_expiry_candidates(
    ticker: str,
    expiry: Optional[str] = None,
) -> Tuple[List[str], List[str], str]:
    normalized_ticker = ticker.upper()
    uw_candidates: List[str] = []
    try:
        with UWClient() as client:
            expiry_breakdown = client.get_expiry_breakdown(normalized_ticker)
        uw_candidates = _sort_expiry_candidates(_extract_expiry_candidates(expiry_breakdown))
    except Exception:
        uw_candidates = []

    uw_candidates = _prepend_expiry(uw_candidates, expiry)
    if uw_candidates:
        return [], uw_candidates, "uw"

    raise HTTPException(status_code=422, detail=f"No expiry available for {normalized_ticker}")


def _compose_expiry_candidates(
    ib_candidates: List[str],
    uw_candidates: List[str],
    max_expiries: int,
) -> List[str]:
    if not ib_candidates:
        return _limit_expiry_candidates(uw_candidates, max_expiries)
    if not uw_candidates:
        return _limit_expiry_candidates(ib_candidates, max_expiries)

    ib_budget = min(4, max_expiries)
    selected = _limit_expiry_candidates(ib_candidates, ib_budget)
    remaining = max_expiries - len(selected)
    if remaining <= 0:
        return selected

    uw_only = [candidate for candidate in uw_candidates if candidate not in selected]
    return selected + _limit_expiry_candidates(uw_only, remaining)


async def _fetch_risk_reversal_history(
    ticker: str,
    timeframe: str,
    delta: int,
    expiry: Optional[str] = None,
    max_expiries: int = 8,
) -> Tuple[List[dict], List[str], str]:
    normalized_ticker = ticker.upper()
    ib_candidates, uw_candidates, expiry_source = await _resolve_expiry_candidates(normalized_ticker, expiry)
    selected_candidates = _compose_expiry_candidates(ib_candidates, uw_candidates, max_expiries)

    last_error: Optional[BaseException] = None
    merged_rows: List[List[dict]] = []
    used_expiries: List[str] = []
    requested_expiry = _normalize_expiry_string(expiry)

    for candidate_expiry in selected_candidates:
        try:
            with UWClient() as client:
                payload = client.get_historical_risk_reversal_skew(
                    normalized_ticker,
                    expiry=candidate_expiry,
                    timeframe=timeframe,
                    delta=delta,
                )
            rows = _normalize_risk_reversal_series(payload)
            if rows:
                merged_rows.append(rows)
                used_expiries.append(candidate_expiry)
        except UWNotFoundError as exc:
            last_error = exc
            if requested_expiry and candidate_expiry == requested_expiry:
                continue
        except UWAPIError as exc:
            last_error = exc
            continue

    merged = _merge_risk_reversal_series(merged_rows)
    if "uw" in expiry_source and _needs_deeper_backfill(merged, timeframe):
        extra_candidates = _limit_expiry_candidates(
            [candidate for candidate in uw_candidates if candidate not in selected_candidates],
            12,
        )
        for candidate_expiry in extra_candidates:
            try:
                with UWClient() as client:
                    payload = client.get_historical_risk_reversal_skew(
                        normalized_ticker,
                        expiry=candidate_expiry,
                        timeframe=timeframe,
                        delta=delta,
                    )
                rows = _normalize_risk_reversal_series(payload)
                if rows:
                    merged_rows.append(rows)
                    used_expiries.append(candidate_expiry)
            except UWAPIError as exc:
                last_error = exc
                continue
        merged = _merge_risk_reversal_series(merged_rows)

    if merged:
        return merged, used_expiries, expiry_source

    if last_error is None:
        raise HTTPException(status_code=502, detail=f"Failed to fetch skew history for {normalized_ticker}")
    raise HTTPException(
        status_code=502,
        detail=getattr(last_error, "args", (f"Failed to fetch skew history for {normalized_ticker}",))[0],
    )


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
    }


# ---------------------------------------------------------------------------
# Dev probes — gated on XENON_API_TEST_MODE or DEV_PROBES=1. Never in prod.
# ---------------------------------------------------------------------------


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


@app.post("/scan")
async def scan():
    """Run watchlist scanner (scanner.py --top 25)."""
    result = await run_entry_point("xenon-scan", ["--top", "25"], timeout=120)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    _write_cache(DATA_DIR / "scanner.json", result.data)
    _write_scan_to_postgres("scanner.json", result.data)
    return result.data


@app.post("/discover")
async def discover():
    """Run market-wide discovery (discover.py --min-alerts 1)."""
    result = await run_entry_point("xenon-discover", ["--min-alerts", "1"], timeout=120)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    if result.data and result.data.get("error"):
        raise HTTPException(status_code=400, detail=result.data["error"])
    _write_cache(DATA_DIR / "discover.json", result.data)
    _write_scan_to_postgres("discover.json", result.data)
    return result.data


@app.post("/flow-analysis")
async def flow_analysis_post(account: str = "ib"):
    """Portfolio flow alignment view for the given broker account.

    POSTs trigger a cache fill on missing tickers (bounded concurrency),
    so the returned payload is always complete. Reads dark-pool and
    options-flow summaries from the shared uw-analyze LRU cache — no
    second UW API pipeline, no stale JSON files on disk.
    """
    from xenon.api.routes.uw_analyze import _runner, get_portfolio_cache
    from xenon.api.services.uw_analyze_portfolio_bias import classify_portfolio

    if account not in ("ib", "futu"):
        raise HTTPException(status_code=400, detail=f"Unknown account: {account!r}")
    cache = get_portfolio_cache()
    data = await classify_portfolio(account=account, cache=cache, runner=_runner, read_only=False)
    return data


@app.get("/flow-analysis")
async def flow_analysis_get(account: str = "ib"):
    """Read-only snapshot of portfolio flow alignment.

    Serves whatever the uw-analyze cache already has without triggering
    a refresh. Used by ``web/lib/useSyncHook`` on initial page load so
    the first render is fast; a subsequent POST fills missing tickers.
    """
    from xenon.api.routes.uw_analyze import get_portfolio_cache
    from xenon.api.services.uw_analyze_portfolio_bias import classify_portfolio

    if account not in ("ib", "futu"):
        raise HTTPException(status_code=400, detail=f"Unknown account: {account!r}")
    cache = get_portfolio_cache()
    data = await classify_portfolio(account=account, cache=cache, runner=None, read_only=True)
    return data


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


def _load_portfolio_view_sync() -> PortfolioView | None:
    """Load the latest scoped portfolio snapshot from Postgres for preflight."""
    scope = _resolve_scope_kwargs()
    try:
        engine = get_sync_engine()
        stmt = (
            select(account_snapshots.c.payload)
            .where(account_snapshots.c.broker == scope["broker"])
            .where(account_snapshots.c.account_env == scope["account_env"])
            .where(account_snapshots.c.broker_account == scope["broker_account"])
            .order_by(account_snapshots.c.snapshot_at.desc())
            .limit(1)
        )
        with engine.connect() as con:
            row = con.execute(stmt).first()
        if row is None or not row.payload:
            return None
        return PortfolioView.model_validate(dict(row.payload))
    except (ValueError, ValidationError) as exc:
        logger.warning("[preflight] Could not validate Postgres portfolio snapshot: %s", exc)
        return None
    except Exception as exc:
        logger.warning("[preflight] Could not load Postgres portfolio snapshot: %s", exc)
        return None


async def _load_portfolio_view() -> PortfolioView | None:
    return await asyncio.to_thread(_load_portfolio_view_sync)


def _body_to_preflight_request(body: dict) -> PreflightRequest:
    """Translate /orders/place body to PreflightRequest. Combo (BAG) orders are skipped
    by preflight in F2 — the TS guard still gates them; server-side BAG gate is scoped
    out of PR-A."""
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


async def _run_preflight(body: dict, user_id: str = "local") -> Verdict:
    if body.get("type") == "combo":
        try:
            req = _body_to_combo_preflight_request(body)
        except (ValueError, ValidationError) as exc:
            return _invalid_order_body_response(exc)
        if preflight.combo_uncovered_short_call_ratio(req) <= 0 or req.action == "SELL":
            return preflight.evaluate_combo(req, PortfolioView())
        portfolio = await _load_portfolio_view()
        if portfolio is None:
            try:
                missing = _portfolio_required_response(body)
            except (ValueError, ValidationError) as exc:
                return _invalid_order_body_response(exc)
            if missing is not None:
                return missing
            portfolio = PortfolioView()
        reservations = orders_store.working_reservations_for(user_id, req.ticker, **_resolve_scope_kwargs())
        return preflight.evaluate_combo(req, portfolio, reservations=reservations)

    try:
        req = _body_to_preflight_request(body)
    except (ValueError, ValidationError) as exc:
        return _invalid_order_body_response(exc)
    if req.action == "BUY":
        return preflight.evaluate(req, PortfolioView())

    portfolio = await _load_portfolio_view()
    if portfolio is None:
        missing = _portfolio_required_response(body)
        if missing is not None:
            return missing
        portfolio = PortfolioView()
    reservations = orders_store.working_reservations_for(user_id, req.ticker, **_resolve_scope_kwargs())
    return preflight.evaluate(req, portfolio, reservations=reservations)


def _lookup_min_tick_via_pool(con_id: int) -> Decimal:
    """Real-path minTick lookup via ib_pool 'data' role. Tests replace
    `_tick_rule_cache` with a deterministic fake.

    TEMPORARY: returns 0.01 (US equity minTick) without hitting IB. The
    real implementation needs an async-safe path: ``ib_insync``'s
    ``reqContractDetails`` starts its own event loop, which collides with
    FastAPI's loop when called from the sync ``quote_guard.check`` stack.
    Stocks are always 0.01; options are 0.01 above $3 and 0.05 below,
    but sub-cent stock ticks are allowed for stocks priced < $1 — out of
    scope for PR-C/D QA.
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
    """Run blocking ib_insync quote work in a worker thread that owns a loop."""
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
            return await asyncio.to_thread(
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
            return await asyncio.to_thread(
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
            return await asyncio.to_thread(
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

    # F2: server-side Gate 4. Run preflight before any subprocess invocation.
    verdict = await _run_preflight(body)
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
    req_row = orders_store.RequestRow(
        ticker=str(body.get("symbol", "")).upper(),
        security_type="STK" if body.get("type") == "stock" else "OPT",
        action=str(body.get("action", "")).upper(),
        quantity=int(body.get("quantity", 0)),
        expiry=body.get("expiry"),
        strike=Decimal(str(body["strike"])) if body.get("strike") is not None else None,
        right=(body.get("right") or "").upper() or None,
        multiplier=int(body.get("multiplier", 100)),
        con_id=int(body.get("con_id") or 0) or None,
        limit_price=Decimal(str(body.get("limitPrice", "0"))),
    )
    outcome = orders_store.reserve_attempt(user_id, cid, req_row, **_resolve_scope_kwargs())
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
        # B6 — always write the literal "IB_REJECT" as reason_code so the UI
        # toast map resolves. The raw IB numeric code + message go into the
        # orders_events audit row so we don't lose the info.
        ib_code = result.data.get("code")
        ib_message = result.data.get("message", "Order failed")
        orders_store.mark_terminal(
            submission_id=submission_id,
            state="REJECTED",
            reason_code=ReasonCode.IB_REJECT.value,
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
    return data


def _try_register_order_from_snapshot(*, perm_id: int, order_id: int) -> bool:
    """Lazy-register a snapshot-only order in orders_store so the modify gate
    can apply. Returns True on insert, False if the order isn't in the
    snapshot or is already registered.

    Source of truth is ``data/orders.json`` — the same file the UI reads.
    Synthetic ib_order_id values (e.g. -5 from snapshot reconstruction) are
    preserved as-is; the modify subprocess uses perm_id as the primary key.
    """
    cache = _read_cache(DATA_DIR / "orders.json")
    if not cache:
        return False
    open_orders = cache.get("open_orders") or []
    target = None
    for o in open_orders:
        if perm_id and o.get("permId") == perm_id:
            target = o
            break
        if order_id and o.get("orderId") == order_id:
            target = o
            break
    if target is None:
        return False
    contract = target.get("contract") or {}
    sec_type = contract.get("secType") or "STK"
    # Multiplier: BAG/OPT default to 100, STK to 1.
    multiplier = 100 if sec_type in ("OPT", "BAG") else 1
    try:
        return orders_store.register_from_snapshot(
            perm_id=str(target.get("permId") or perm_id),
            ib_order_id=str(target.get("orderId") or order_id),
            ticker=str(target.get("symbol") or contract.get("symbol") or ""),
            security_type=sec_type,
            action=str(target.get("action") or "BUY"),
            quantity=int(target.get("totalQuantity") or 0),
            limit_price=float(target.get("limitPrice") or 0.0),
            multiplier=multiplier,
            **_resolve_scope_kwargs(),
        )
    except Exception as exc:
        logger.warning(
            "register_from_snapshot failed for perm_id=%s: %s",
            perm_id,
            exc,
        )
        return False


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
    body = await request.json()
    return await _orders_modify_from_body(body)


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
    if not order_id and perm_id:
        seq_outcome = orders_store.apply_modify_by_perm_id(str(perm_id), modify_sequence, **_scope)
    else:
        seq_outcome = orders_store.apply_modify(str(order_id), modify_sequence, **_scope)
    # If the perm_id is unknown to orders_store but exists in the IB snapshot,
    # lazy-register it and retry. Covers orders placed before orders_store
    # tracking existed or by an external client (the snapshot reconstruction
    # gives us synthetic ib_order_id like -5 plus the real perm_id).
    if not seq_outcome["applied"] and seq_outcome["current_sequence"] == -1:
        if _try_register_order_from_snapshot(perm_id=perm_id, order_id=order_id):
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


@app.post("/cta/share")
async def cta_share():
    """Generate CTA X share report (4 cards + preview HTML). Returns output path."""
    result = await run_entry_point("xenon-generate-cta-share", ["--json", "--no-open"], timeout=120)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    return result.data


@app.post("/regime/scan")
async def regime_scan():
    """Run CRI scan (cri_scan.py --json). 120s timeout."""
    result = await run_entry_point("xenon-cri-scan", ["--json"], timeout=120)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    _write_cache(DATA_DIR / "cri.json", result.data)
    _write_scan_to_postgres("cri.json", result.data)
    return result.data


# ── VCG (Volatility-Credit Gap) ─────────────────────────────────────

_vcg_last_scan: float = 0.0
_vcg_scan_lock: Optional[asyncio.Lock] = None
VCG_COOLDOWN_S = 60


def _is_market_open_now() -> bool:
    """Live ET market-hours check — used to override stamped market_open at read time."""
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    et = _dt.now(tz=ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return False
    minutes = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


@app.get("/vcg")
async def vcg_get():
    """Return the latest VCG snapshot from Postgres.

    Sources from `xenon.vcg_series.payload` — the same JSONB the scanner
    writes via `save_vcg_scan`. Replaces the prior `data/vcg.json` reader
    in `web/app/api/vcg/route.ts`. Background refresh is still triggered
    via `POST /vcg/scan` (the cron-or-manual path), so this endpoint is
    purely read-side.

    Returns an empty-shape envelope when no scan rows exist yet so the
    web route can render the panel without 404 handling.
    """
    from xenon.db.engine import get_engine
    from xenon.db.queries.scans import get_latest_vcg

    engine = get_engine()
    async with engine.connect() as conn:
        payload = await get_latest_vcg(conn)

    market_open_now = _is_market_open_now()
    if not payload:
        return {
            "scan_time": "",
            "market_open": market_open_now,
            "credit_proxy": "HYG",
            "signal": {},
            "history": [],
        }
    payload["market_open"] = market_open_now
    return payload


async def _load_latest_vcg_from_pg() -> dict | None:
    """Helper: read the latest vcg_series payload from Postgres."""
    from xenon.db.engine import get_engine
    from xenon.db.queries.scans import get_latest_vcg

    try:
        engine = get_engine()
        async with engine.connect() as conn:
            return await get_latest_vcg(conn)
    except Exception:
        logger.warning("[vcg] failed to read latest payload from PG", exc_info=True)
        return None


@app.post("/vcg/scan")
async def vcg_scan():
    """Run VCG scan (vcg_scan.py --json). 60s cooldown between scans.

    During the cooldown window, returns the most recent payload from
    xenon.vcg_series (Postgres) instead of re-reading data/vcg.json.
    """
    global _vcg_last_scan, _vcg_scan_lock
    import time as _time

    if _vcg_scan_lock is None:
        _vcg_scan_lock = asyncio.Lock()
    now = _time.monotonic()
    if now - _vcg_last_scan < VCG_COOLDOWN_S:
        cached = await _load_latest_vcg_from_pg()
        if cached:
            return cached
    async with _vcg_scan_lock:
        if _time.monotonic() - _vcg_last_scan < VCG_COOLDOWN_S:
            cached = await _load_latest_vcg_from_pg()
            if cached:
                return cached
        result = await run_entry_point("xenon-vcg-scan", ["--json"], timeout=120)
        if not result.ok:
            raise HTTPException(status_code=502, detail=result.error)
        _write_cache(DATA_DIR / "vcg.json", result.data)
        _write_scan_to_postgres("vcg.json", result.data)
        _vcg_last_scan = _time.monotonic()
        return result.data


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


@app.post("/vcg/share")
async def vcg_share():
    """Generate VCG X share report (4 cards + preview HTML). Returns output path."""
    result = await run_entry_point("xenon-generate-vcg-share", ["--json", "--no-open"], timeout=120)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    return result.data


# ── GEX (Gamma Exposure Levels) ─────────────────────────────────────

_gex_last_scan: float = 0.0
_gex_scan_lock: Optional[asyncio.Lock] = None
GEX_COOLDOWN_S = 60


@app.post("/gex/share")
async def gex_share():
    """Generate GEX X share report (4 cards + preview HTML). Returns output path."""
    result = await run_entry_point("xenon-generate-gex-share", ["--json", "--no-open"], timeout=120)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    return result.data


@app.post("/gex/scan")
async def gex_scan(ticker: str = "SPX"):
    """Run GEX scan (gex_scan.py --json --ticker X). 60s cooldown between scans."""
    global _gex_last_scan, _gex_scan_lock
    import time as _time

    if _gex_scan_lock is None:
        _gex_scan_lock = asyncio.Lock()
    now = _time.monotonic()
    if now - _gex_last_scan < GEX_COOLDOWN_S:
        cached = _read_cache(DATA_DIR / "gex.json")
        if cached:
            return cached
    async with _gex_scan_lock:
        if _time.monotonic() - _gex_last_scan < GEX_COOLDOWN_S:
            cached = _read_cache(DATA_DIR / "gex.json")
            if cached:
                return cached
        result = await run_entry_point("xenon-gex-scan", ["--json", "--ticker", ticker.upper()], timeout=120)
        if not result.ok:
            raise HTTPException(status_code=502, detail=result.error)
        _write_cache(DATA_DIR / "gex.json", result.data)
        _write_scan_to_postgres("gex.json", result.data)
        _gex_last_scan = _time.monotonic()
        return result.data


@app.post("/regime/share")
async def regime_share():
    """Generate Regime/CRI X share report (4 cards + preview HTML). Returns output path."""
    result = await run_entry_point("xenon-generate-regime-share", ["--json", "--no-open"], timeout=120)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    return result.data


@app.post("/internals/share")
async def internals_share():
    """Generate internals share report using the shared CRI report builder."""
    result = await run_entry_point("xenon-generate-regime-share", ["--json", "--no-open"], timeout=120)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    return result.data


@app.get("/internals/skew-history")
async def internals_skew_history(
    nq_ticker: str = Query(default="NDX"),
    spx_ticker: str = Query(default="SPX"),
    timeframe: str = Query(default="5Y"),
    nq_delta: int = Query(default=25),
    spx_delta: int = Query(default=25),
    nq_expiry: Optional[str] = None,
    spx_expiry: Optional[str] = None,
):
    if not uw_available:
        raise HTTPException(status_code=503, detail="UW token is required for internals skew history")

    normalized_timeframe = timeframe.upper().strip() or "5Y"
    cache_path = _build_internals_skew_cache_path(
        nq_ticker,
        spx_ticker,
        normalized_timeframe,
        nq_delta,
        spx_delta,
        nq_expiry,
        spx_expiry,
    )
    cached = _read_internals_skew_cache(cache_path)
    if cached:
        return cached

    try:
        nq_rows, used_nq_expiries, nq_expiry_source = await _fetch_risk_reversal_history(
            nq_ticker,
            normalized_timeframe,
            nq_delta,
            nq_expiry,
            max_expiries=12,
        )
        spx_rows, used_spx_expiries, spx_expiry_source = await _fetch_risk_reversal_history(
            spx_ticker,
            normalized_timeframe,
            spx_delta,
            spx_expiry,
            max_expiries=12,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    payload = _internals_skew_cache_payload(
        nq_ticker,
        spx_ticker,
        normalized_timeframe,
        nq_delta,
        spx_delta,
        nq_expiry,
        spx_expiry,
        nq_rows,
        spx_rows,
        used_nq_expiries,
        used_spx_expiries,
    )
    payload["nq"]["expiry_source"] = nq_expiry_source
    payload["spx"]["expiry_source"] = spx_expiry_source
    _write_cache(cache_path, payload)
    return payload


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
    if blotter_has_trades(pg_payload):
        return pg_payload

    result = await run_module("xenon.trade_blotter.flex_query", ["--json"], timeout=120)
    if not result.ok:
        is_unconfigured = result.exit_code == 2 or (result.error and "FLEX_NOT_CONFIGURED" in result.error)
        if is_unconfigured:
            payload = {
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
            _write_cache(DATA_DIR / "blotter.json", payload)
            return payload
        raise HTTPException(status_code=502, detail=result.error)
    payload = {**result.data, "configured": True, "source": "flex"}
    _write_cache(DATA_DIR / "blotter.json", payload)
    return payload


# ---------------------------------------------------------------------------
# Performance — task registry for deduplication (single-worker assumed)
# ---------------------------------------------------------------------------
_running_build: Optional[asyncio.Task] = None


async def _do_performance_rebuild() -> dict:
    """Run portfolio_performance.py and cache result."""
    result = await run_entry_point("xenon-portfolio-perf", ["--json"], timeout=180)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    _write_cache(DATA_DIR / "performance.json", result.data)
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
    args = ["--symbol", symbol.upper()]
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
    result = await run_entry_point("xenon-ib-option-chain", ["--symbol", symbol.upper()], timeout=15)
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
