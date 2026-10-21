"""System routes: /health, /admin/operator, the dev rehydrate probe and the WebSocket ticket endpoints.

Health helpers read process-wide state (ib_pool, order_owner, uw_available, app.state) from server.py at
call time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import func, select

from xenon.api import trading_mode
from xenon.api.guards import mask_account
from xenon.api.ib_gateway import check_ib_gateway
from xenon.api.routes import futu as futu_routes
from xenon.api.services import order_entry
from xenon.api.services.flex_token import flex_token_status
from xenon.api.ws_ticket import create_ticket, validate_ticket
from xenon.db.engine import get_sync_engine
from xenon.db.schema import account_snapshots, order_events, order_submissions
from xenon.execution import gate4

router = APIRouter()
logger = logging.getLogger("xenon.api")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def _compute_futu_health() -> dict[str, Any]:
    """Derive the Futu health block from the singleton + cached file.

    Never probes OpenD (tribunal T11) — reachability comes from the last
    cached sync result, not a TCP probe. Cheap enough to compute per
    /health call.
    """
    from xenon.api import server

    cached_path = server.DATA_DIR / "futu_portfolio.json"
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
        "connected": futu_routes._futu_client is not None and futu_routes._futu_client.is_connected(),
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

        kwargs = order_entry._resolve_scope_kwargs()
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

        kwargs = order_entry._resolve_scope_kwargs()
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


@router.get("/health")
async def health():
    from xenon.api import server

    gw = await check_ib_gateway()
    owner = server.order_owner.status() if server.order_owner is not None else None
    return {
        # Stays HTTP 200 (a failing healthcheck restart would clear the latch);
        # a latched ORDERS_HALTED turns the status red.
        "status": "orders_halted" if owner and owner["halted"] else "ok",
        "test_mode": server._is_test_mode(),
        "ib_gateway": gw,
        "ib_pool": server.ib_pool.status() if server.ib_pool else {},
        "orders_owner": owner,
        "uw": server.uw_available,
        "futu": _compute_futu_health(),
        "trading_mode": getattr(server.app.state, "trading_mode", trading_mode.MODE),
        # /health is auth-exempt — mask the IB account so the public payload
        # does not leak the full identifier. The raw value stays on app.state
        # for the require_mode_verified 503 detail (which is auth-gated).
        "account": mask_account(getattr(server.app.state, "account", "")),
        "mode_verified": getattr(server.app.state, "mode_verified", False),
        "snapshotter": _snapshotter_health(),
        "order_submissions": _order_submissions_health(),
        "flex_divergence": _flex_divergence_health(),
        "flex_token": flex_token_status(),
        "realtime_subscribers": await asyncio.to_thread(_realtime_subscribers_health),
    }


@router.get("/admin/operator")
async def admin_operator():
    """Operator console aggregate. Read-only. No per-route Depends — gated by
    the global auth_middleware like every data route (a per-route Depends would
    401 cross-container in Docker since the Next proxy forwards no token). The
    DB helpers are called inline (not via to_thread) to match /health and to
    stay on the Phase-2 test connection."""
    from xenon.api import server

    gw = await check_ib_gateway()
    pool = server.ib_pool.status() if server.ib_pool else {}
    scope = order_entry._resolve_scope_kwargs()
    return {
        "generated_at": _iso_datetime(datetime.now(timezone.utc)),
        "ib_gateway": gw,
        "ib_pool": pool,
        "ib_auth": _ib_auth_verdict(gw, pool),
        "trading_mode": getattr(server.app.state, "trading_mode", trading_mode.MODE),
        "account": mask_account(getattr(server.app.state, "account", "")),
        "mode_verified": getattr(server.app.state, "mode_verified", False),
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
    from xenon.api import server

    return server._is_test_mode() or os.environ.get("DEV_PROBES", "").lower() in {
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


@router.post("/dev/rehydrate/synthetic", include_in_schema=False)
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
        **order_entry._resolve_scope_kwargs(),
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


@router.post("/ws-ticket")
async def get_ws_ticket(request: Request):
    """Issue a short-lived ticket for WebSocket authentication.

    The middleware already authorized this request (internal token or scoped
    API key) and set request.state.user. Reading it here avoids re-validating.
    """
    user = getattr(request.state, "user", None) or {}
    ticket = create_ticket(user.get("sub", "unknown"))
    return {"ticket": ticket}


@router.post("/ws-ticket/validate")
async def validate_ws_ticket(request: Request):
    """Validate a WebSocket ticket (called by the Node.js relay). Internal only."""
    body = await request.json()
    ticket = body.get("ticket", "")
    user_id = validate_ticket(ticket)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired ticket")
    return {"user_id": user_id}
