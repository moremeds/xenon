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

# Project paths — file lives at src/xenon/api/server.py
PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = PROJECT_ROOT / "src"
DATA_DIR = PROJECT_ROOT / "data"
INTERNALS_SKEW_CACHE_DIR = DATA_DIR / "cache"
INTERNALS_SKEW_CACHE_TTL_SECONDS = 60 * 15

from xenon.api.auth import verify_api_key, verify_clerk_jwt
from xenon.api.ib_gateway import check_ib_gateway, ensure_ib_gateway, is_cloud_mode, is_docker_mode, restart_ib_gateway
from xenon.api.ib_pool import IBPool
from xenon.api.pool_order_manage import pool_cancel_order, pool_modify_order
from xenon.api.routes.historical import router as historical_router
from xenon.api.routes.uw_analyze import router as uw_analyze_router
from xenon.api.routes.uw_stats import router as uw_stats_router
from xenon.api.subprocess import ScriptResult, run_entry_point, run_module
from xenon.api.ws_ticket import create_ticket, validate_ticket
from xenon.clients.ib_client import DEFAULT_GATEWAY_PORT
from xenon.execution import orders_store, preflight, quote_guard, quote_tokens
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
from ib_insync import Index

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
test_mode: bool = os.environ.get("XENON_API_TEST_MODE", "").lower() in {"1", "true", "yes", "on"}
test_order_counter: int = 900000


def _next_test_order_ids() -> tuple[int, int]:
    global test_order_counter
    test_order_counter += 1
    order_id = test_order_counter
    perm_id = 8_000_000 + order_id
    return order_id, perm_id


async def _trend_scan_premarket_loop():
    """Run trend scanner at 8:30 AM ET on weekdays."""
    import zoneinfo

    et = zoneinfo.ZoneInfo("America/New_York")
    while True:
        now = datetime.now(et)
        target_hour, target_min = 8, 30
        target = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        while target.weekday() >= 5:
            target += timedelta(days=1)
        wait_secs = (target - now).total_seconds()
        logger.info("Trend scan scheduled for %s (in %.0fs)", target, wait_secs)
        await asyncio.sleep(wait_secs)
        try:
            result = await run_entry_point("xenon-trend-scan", ["--top", "25"], timeout=180)
            if result.ok:
                _write_cache(DATA_DIR / "trend_scan.json", result.data)
                logger.info("Pre-market trend scan complete: %d candidates", len(result.data.get("candidates", [])))
            else:
                logger.warning("Pre-market trend scan failed: %s", result.error)
        except Exception:
            logger.warning("Pre-market trend scan error", exc_info=True)


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

    db_path = os.environ.get("XENON_ORDERS_DB_PATH") or str(_orders_store_mod._resolve_path(None))

    try:
        await asyncio.wait_for(
            asyncio.to_thread(
                _rehydrate_mod.rehydrate_on_boot,
                ib_client_factory=_ib_client_factory,
                orders_store=_orders_store_mod,
                db_path=db_path,
            ),
            timeout=10.0,
        )
        logger.info("single_leg rehydrate completed on boot")
    except asyncio.TimeoutError:
        logger.warning("single_leg rehydrate timed out after 10s; continuing to serve")
    except Exception as exc:  # noqa: BLE001
        logger.warning("single_leg rehydrate failed on boot; continuing to serve: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start IB pool and UW client on startup, tear down on shutdown."""
    global ib_pool, uw_available

    if test_mode:
        logger.info("Xenon API starting in test mode; IB Gateway and pool startup are disabled")
        uw_available = bool(os.environ.get("UW_TOKEN"))
        orders_store.init_store()
        await _run_rehydrate_on_boot()
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

    orders_store.init_store()

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

    # Trend scanner (8:30 AM ET weekdays)
    _trend_scan_task = None
    if os.environ.get("XENON_DAILY_JOB_WORKER_ID", "0") == "0":
        _trend_scan_task = asyncio.create_task(_trend_scan_premarket_loop())

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
        if _trend_scan_task is not None:
            _trend_scan_task.cancel()
            try:
                await _trend_scan_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
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
        logger.info("Xenon API shut down")


app = FastAPI(title="Xenon API", version="1.0.0", lifespan=lifespan)
app.include_router(historical_router)
app.include_router(uw_analyze_router)
app.include_router(uw_stats_router)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://localhost:3000|http://127\.0\.0\.1:3000",
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth middleware — protect all routes except /health and internal ticket validation
AUTH_EXEMPT_PATHS = {"/health", "/ws-ticket/validate", "/docs", "/openapi.json"}


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
        raise


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
        "test_mode": test_mode,
        "ib_gateway": gw,
        "ib_pool": ib_pool.status() if ib_pool else {},
        "uw": uw_available,
        "futu": _compute_futu_health(),
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
    return result.data


@app.post("/trend-scan")
async def trend_scan():
    """Run 3-stage trend scanner (trend_scan.py --top 25)."""
    result = await run_entry_point("xenon-trend-scan", ["--top", "25"], timeout=180)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    _write_cache(DATA_DIR / "trend_scan.json", result.data)
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


@app.post("/portfolio/sync")
async def portfolio_sync():
    """Sync portfolio from IB via subprocess.

    Scripts auto-allocate client IDs from subprocess range (20-49).
    Auto-restarts IB Gateway on ECONNREFUSED and retries once.
    """
    result = await _run_ib_script_with_recovery(
        "xenon-ib-sync", ["--sync", "--port", str(DEFAULT_GATEWAY_PORT)], timeout=30
    )
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    # ib_sync.py writes to data/portfolio.json; read it back
    from xenon.utils.atomic_io import verified_load

    try:
        data = verified_load(str(DATA_DIR / "portfolio.json"))
        return data
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to read synced portfolio: {e}")


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


@app.post("/orders/refresh")
async def orders_refresh():
    """Sync orders from IB via subprocess.

    Scripts auto-allocate client IDs from subprocess range (20-49).
    Auto-restarts IB Gateway on ECONNREFUSED and retries once.
    """
    if test_mode:
        return {"status": "ok", "orders": []}

    result = await _run_ib_script_with_recovery(
        "xenon-ib-orders", ["--sync", "--port", str(DEFAULT_GATEWAY_PORT)], timeout=30
    )
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    # ib_orders.py writes to data/orders.json; read it back
    cache = _read_cache(DATA_DIR / "orders.json")
    if cache:
        return cache
    raise HTTPException(status_code=502, detail="Failed to read synced orders")


# ---------------------------------------------------------------------------
# Phase 3: IB order operations
# ---------------------------------------------------------------------------


def _load_portfolio_view() -> PortfolioView | None:
    """Load portfolio snapshot for preflight. Matches TS guard's data/portfolio.json source.

    Returns None when the snapshot is missing or unreadable — the caller
    must fail OPEN in that case to match web/app/api/orders/place/route.ts
    which logs and skips enforcement rather than blocking every SELL on
    a fresh/cleaned environment. F5 will replace this with a live IB-pool
    call per SL spec §5.2, at which point the fail-open branch disappears.
    """
    data_dir = Path(os.environ.get("XENON_DATA_DIR", str(DATA_DIR)))
    pf_file = data_dir / "portfolio.json"
    if not pf_file.exists():
        return None
    try:
        raw = json.loads(pf_file.read_text())
        return PortfolioView.model_validate(raw)
    except (OSError, ValueError, ValidationError) as exc:
        logger.warning("[preflight] Could not load portfolio.json: %s", exc)
        return None


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


def _run_preflight(body: dict, user_id: str = "local") -> Verdict:
    if body.get("type") == "combo":
        return Verdict(accept=True)
    portfolio = _load_portfolio_view()
    if portfolio is None:
        # Fail OPEN to match TS guard (route.ts:183-185). Without a portfolio
        # snapshot we can't distinguish covered from naked shorts; blocking
        # every SELL would regress fresh-start workflows. F5 (live IB pool)
        # removes this branch.
        return Verdict(accept=True)
    req = _body_to_preflight_request(body)
    reservations = orders_store.working_reservations_for(user_id, req.ticker)
    return preflight.evaluate(req, portfolio, reservations=reservations)


def _lookup_min_tick_via_pool(con_id: int) -> Decimal:
    """Real-path minTick lookup via ib_pool 'data' role. Tests replace
    `_tick_rule_cache` with a deterministic fake."""
    raise HTTPException(status_code=503, detail="IB data role not ready")


_tick_rule_cache = quote_guard.TickRuleCache(
    source=_lookup_min_tick_via_pool,
    ttl_seconds=24 * 3600,
)


def _now() -> datetime:
    """Test seam: override via monkeypatch to inject a fixed RTH timestamp."""
    return datetime.now(tz=timezone.utc)


def _fetch_quote_snapshot(ticker: str, con_id: int) -> dict:
    """Fetch a bid/ask snapshot from the ib_pool 'data' role.

    Raises HTTPException(503) if the data role is unavailable. Tests
    monkeypatch this symbol on `xenon.api.server`.
    """
    pool = ib_pool
    if pool is None:
        raise HTTPException(status_code=503, detail="IB data role unavailable")
    raise HTTPException(status_code=503, detail="IB data role unavailable")


@app.get("/orders/quote")
async def orders_quote(ticker: str, con_id: int):
    secret = os.environ.get("XENON_QUOTE_TOKEN_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="quote secret not configured")
    snap = _fetch_quote_snapshot(ticker, con_id)
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
    body = await request.json()

    # F2: server-side Gate 4. Run preflight before any subprocess invocation.
    verdict = _run_preflight(body)
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
        token = body.get("quote_token")
        if not token:
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "quote_token is required",
                    "reason_code": ReasonCode.STALE_QUOTE.value,
                },
            )
        qv = quote_guard.check(
            token=token,
            token_secret=os.environ.get("XENON_QUOTE_TOKEN_SECRET", ""),
            con_id=int(body.get("con_id") or 0),
            ticker=str(body.get("symbol", "")).upper(),
            security_type="STK" if body.get("type") == "stock" else "OPT",
            action=str(body.get("action", "")).upper(),
            limit_price=Decimal(str(body.get("limitPrice", "0"))),
            now=_now(),
            tick_rule_lookup=_tick_rule_cache.get,
        )
        _override_detail = None
        if not qv.accept:
            if qv.reason_code == ReasonCode.LIMIT_OUT_OF_BAND and body.get("acknowledge_limit_override") is True:
                _override_detail = {
                    "reason_detail": qv.reason_detail,
                    "limit_price": body.get("limitPrice"),
                }
            else:
                return JSONResponse(
                    status_code=400,
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
    outcome = orders_store.reserve_attempt(user_id, cid, req_row)
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

    if test_mode:
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
        orders_store.mark_terminal(
            submission_id=submission_id,
            state="REJECTED",
            reason_code=str(result.data.get("code") or "IB_REJECT"),
            filled_qty=0,
            avg_fill_price=None,
        )
        raise HTTPException(status_code=502, detail=result.data.get("message", "Order failed"))
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


def _record_manage_event(ib_order_id: str, kind: str, detail: dict) -> None:
    """Write orders_events row for a cancel/modify attempt.

    Looks up submission by ib_order_id. If no submission exists (order placed
    pre-F4, or before a reserve_attempt row was created), the event is
    skipped — orders_events.submission_id is NOT NULL and has no synthetic
    parent row available. This is documented behaviour for legacy orders.
    """
    try:
        sid = orders_store.lookup_submission_id_by_ib_order_id(ib_order_id)
        if sid:
            orders_store.record_event(sid, kind, detail)
    except Exception:  # pragma: no cover — event writes are best-effort
        logger.warning("Failed to record %s event for order %s", kind, ib_order_id, exc_info=True)


@app.post("/orders/cancel")
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
    if test_mode:
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
        _record_manage_event(str(order_id or ""), "CANCEL", detail)
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
        _record_manage_event(str(order_id or ""), "CANCEL", detail)
        raise HTTPException(status_code=http_status, detail=detail)

    _record_manage_event(
        str(order_id or ""),
        "CANCEL",
        {"status": data.get("status"), "message": data.get("message"), "http_status": 200},
    )
    return data


@app.post("/orders/modify")
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
    if test_mode:
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

    # Apply sequence gate BEFORE spawning the subprocess
    seq_outcome = orders_store.apply_modify(str(order_id), modify_sequence)
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
        _record_manage_event(str(order_id or ""), "MODIFY", detail)
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
        _record_manage_event(str(order_id or ""), "MODIFY", detail)
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
    )
    return data


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
    return result.data


# ── VCG (Volatility-Credit Gap) ─────────────────────────────────────

_vcg_last_scan: float = 0.0
_vcg_scan_lock: Optional[asyncio.Lock] = None
VCG_COOLDOWN_S = 60


@app.post("/vcg/scan")
async def vcg_scan():
    """Run VCG scan (vcg_scan.py --json). 60s cooldown between scans."""
    global _vcg_last_scan, _vcg_scan_lock
    import time as _time

    if _vcg_scan_lock is None:
        _vcg_scan_lock = asyncio.Lock()
    now = _time.monotonic()
    if now - _vcg_last_scan < VCG_COOLDOWN_S:
        cached = _read_cache(DATA_DIR / "vcg.json")
        if cached:
            return cached
    async with _vcg_scan_lock:
        if _time.monotonic() - _vcg_last_scan < VCG_COOLDOWN_S:
            cached = _read_cache(DATA_DIR / "vcg.json")
            if cached:
                return cached
        result = await run_entry_point("xenon-vcg-scan", ["--json"], timeout=120)
        if not result.ok:
            raise HTTPException(status_code=502, detail=result.error)
        _write_cache(DATA_DIR / "vcg.json", result.data)
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
async def blotter_sync():
    """Run IB Flex Query for historical trades. 120s timeout."""
    result = await run_module("xenon.trade_blotter.flex_query", ["--json"], timeout=120)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    _write_cache(DATA_DIR / "blotter.json", result.data)
    return result.data


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
