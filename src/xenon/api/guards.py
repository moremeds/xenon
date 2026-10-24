"""FastAPI-tier helpers for the trading-mode switch.

Lives separate from `xenon.api.trading_mode` so the config layer (used by
`xenon.clients.ib_client` for port derivation) does NOT need to import
fastapi. Only routes/lifespan code touches this module.
"""

from __future__ import annotations

import logging
import os

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from xenon.api import trading_mode
from xenon.execution import broker_capabilities
from xenon.execution.account_scope import AccountScope, resolve_from_app_state
from xenon.execution.preflight import ReasonCode

logger = logging.getLogger(__name__)


def is_read_only() -> bool:
    """True when XENON_READ_ONLY=1 — set by `scripts/infra/dev.sh live`.

    Lets dev sessions hit live IB for debugging without persisting fills,
    snapshots, journal entries, or orders. Real live trading goes through
    the macmini Docker stack (writes core_dev). See
    docs/runbooks/dev-prod-db-cutover.md.
    """
    return os.environ.get("XENON_READ_ONLY") == "1"


def read_only_403() -> JSONResponse:
    """Standard 403 for write routes when read-only mode is active.

    Top-level `reason_code` so the web toast helpers can render it
    directly. HTTPException(detail={...}) would nest it under .detail
    and break the toast — see CLAUDE.md feedback memory
    `httpexception_dict_detail_breaks_toast`.
    """
    return JSONResponse(
        status_code=403,
        content={
            "reason_code": "READ_ONLY_MODE",
            "detail": (
                "Write disabled: this session was started with XENON_READ_ONLY=1 "
                "(typically `dev.sh live`). Use the macmini Docker stack for prod "
                "writes, or restart in paper mode for dev writes."
            ),
        },
    )


def mask_account(account: str | None) -> str:
    """Mask an IB account for public display on /health (auth-exempt path).

    Rules: empty → empty; ≤4 chars → unchanged (nothing useful to mask);
    5-8 chars → first 2 + "***" (full last-4 would leak the whole string);
    >8 chars → first 2 + "***" + last 4. The output is never longer than
    the input and never contains the original as a substring.
    """
    if not account:
        return ""
    n = len(account)
    if n <= 4:
        return account
    if n <= 8:
        return account[:2] + "***"
    return account[:2] + "***" + account[-4:]


def _check_mode_verified(state) -> None:
    """Mode-mismatch check shared by `require_mode_verified` and
    `ensure_order_allowed`. `state` is an app.state-like object carrying
    `{trading_mode, account, mode_verified}`."""
    if getattr(state, "mode_verified", False):
        return
    declared = getattr(state, "trading_mode", trading_mode.MODE)
    observed = getattr(state, "account", "")
    raise HTTPException(
        status_code=503,
        detail=(
            f"Trading mode mismatch: .env declares XENON_TRADING_MODE={declared!r} "
            f"but IB Gateway is logged in as account={observed!r} "
            f"(expected prefix {trading_mode.EXPECTED_PREFIX!r}). "
            f"Fix: align .env with the Gateway login and restart."
        ),
    )


def require_mode_verified(request: Request) -> None:
    """FastAPI dependency: reject order-mutating requests when mode is unverified.

    Reads `app.state.{trading_mode,account,mode_verified}` populated by the
    server lifespan guard. Returns 503 with both the declared mode and the
    observed account in the body so the operator can fix the mismatch
    (edit .env or relog Gateway). The 503 detail is auth-gated (only routes
    behind auth call this), so leaking the raw account here is fine.
    """
    _check_mode_verified(request.app.state)


_KIND_TO_OP = {
    "order placement": "place_single",
    "order cancellation": "cancel",
    "order modification": "modify",
    "order replacement": "replace",
    "naked-short audit cancel": "cancel",
}


def _op_for_kind(kind: str) -> str | None:
    """Capability operation for an `ensure_order_allowed` kind; None if unknown."""
    if kind.startswith("wizard"):
        return "wizard"
    return _KIND_TO_OP.get(kind)


def _broker_403(broker: str, kind: str, cause: str) -> JSONResponse:
    message = f"{broker} is read-only for {kind}" + (f": {cause}" if cause else "")
    return JSONResponse(
        status_code=403,
        content={
            "detail": message,
            "reason_code": ReasonCode.READ_ONLY_BROKER.value,
            "reason_detail": message,
        },
    )


def broker_capability_denial(
    broker: str, op: str | None, kind: str, account_env: str | None = None
) -> JSONResponse | None:
    """Capability check for one broker operation (`broker_capabilities`).

    `XENON_READ_ONLY=1` wins first (403 `READ_ONLY_MODE`). A denied
    capability → 403 JSONResponse with top-level `reason_code`
    `READ_ONLY_BROKER`. Returns None when the operation is allowed.
    `account_env` is the FUTU request scope's environment.
    """
    if is_read_only():
        return read_only_403()
    ok, _, cause = broker_capabilities.evaluate(broker, op, account_env)
    return None if ok else _broker_403(str(broker or "").upper(), kind, cause)


# -- FUTU request scope (§11 A2, A3) ------------------------------------------

FUTU_SCOPE_REQUIRED = "FUTU_SCOPE_REQUIRED"
FUTU_SCOPE_INVALID = "FUTU_SCOPE_INVALID"
FUTU_SCOPE_CHANGE_PENDING = "FUTU_SCOPE_CHANGE_PENDING"
# broker_account of a FUTU read scope whose env has no account: every scoped
# query then matches nothing, so reads return empty lists.
FUTU_NOT_CONFIGURED_ACCOUNT = ""


class FutuScopeRefused(Exception):
    """Raised by FUTU scope dependencies; server.py maps it to a JSONResponse
    with a top-level `reason_code`."""

    def __init__(self, status_code: int, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.status_code, self.reason_code, self.detail = status_code, reason_code, detail

    def response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code, content={"detail": self.detail, "reason_code": self.reason_code}
        )


def futu_not_configured(scope: AccountScope) -> bool:
    return scope.broker == "FUTU" and scope.broker_account == FUTU_NOT_CONFIGURED_ACCOUNT


def futu_read_env(request: Request) -> str:
    """`account_env` from the query (the only wire name, GET and POST alike).
    Missing → the default read env; invalid → 400 `FUTU_SCOPE_INVALID`."""
    from xenon.execution import futu_scope

    raw = request.query_params.get("account_env")
    if raw is None:
        return futu_scope.default_read_env()
    env = futu_scope.parse_account_env(raw)
    if env is None:
        raise FutuScopeRefused(400, FUTU_SCOPE_INVALID, "account_env must be paper or live")
    return env


def _futu_scope_required() -> JSONResponse:
    return FutuScopeRefused(400, FUTU_SCOPE_REQUIRED, "Futu order requests need ?account_env=paper|live").response()


_pending_logged: set[tuple] = set()


def futu_scope_change_pending(account_env: str) -> list[tuple[str, str]]:
    """Stored FUTU scopes of `account_env` with unresolved rows that are not
    the configured account of that env (§11 A2). Empty → no veto. Toggling
    between the two configured scopes never produces an entry."""
    from xenon.execution import futu_scope, orders_store

    configured, _ = futu_scope.configured_scope(account_env)
    current = configured.broker_account if configured is not None else None
    return [s for s in orders_store.futu_unresolved_scopes() if s[0] == account_env and s[1] != current]


def _futu_change_pending_denial(account_env: str, kind: str) -> JSONResponse | None:
    stale = futu_scope_change_pending(account_env)
    if not stale:
        return None
    key = (account_env, tuple(stale))
    if key not in _pending_logged:
        _pending_logged.add(key)
        logger.error(
            "FUTU_SCOPE_CHANGE_PENDING: %d stored %s Futu scope(s) other than the configured account still "
            "have unresolved orders; Futu %s for %s is refused until they are resolved or reconciled",
            len(stale),
            account_env,
            kind,
            account_env,
        )
    message = (
        f"Futu {account_env} account changed while orders of the previous account are unresolved; resolve them first"
    )
    return JSONResponse(
        status_code=403,
        content={"detail": message, "reason_code": FUTU_SCOPE_CHANGE_PENDING, "reason_detail": message},
    )


def ensure_order_allowed(
    kind: str, state, op: str | None = None, *, account_env: str | None = None
) -> JSONResponse | None:
    """Single guard for every order-mutating path.

    Called at the top of each order write entry point (HTTP routes and the
    in-process `_orders_*_from_body` helpers the wizard reaches without
    going through a route). Checks, in order:

    1. `XENON_READ_ONLY=1` → 403 `READ_ONLY_MODE` JSONResponse.
    2. Non-IB `app.state.broker` → capability check
       (`broker_capability_denial`; `op` defaults to the operation `kind`
       names) → 403 `READ_ONLY_BROKER` JSONResponse. FUTU first needs the
       request's `account_env` (missing / invalid → 400
       `FUTU_SCOPE_REQUIRED`), and after the capability the per-env scope
       change veto (403 `FUTU_SCOPE_CHANGE_PENDING`, §11 A2). An allowed
       FUTU operation (place_single, cancel, modify) returns None: the core
       dispatches it to the Futu order worker. Broker denial precedes
       the mode check: FUTU carries no IB trading mode to verify (see
       test_place_quote_gate.py
       `test_futu_scope_rejects_before_ib_mode_verification`).
    3. Unverified trading mode → HTTPException 503 (same detail as
       `require_mode_verified`).

    `kind` names the operation in denial copy ("order placement").
    `state` is the FastAPI `app.state` — `request.app.state` in routes,
    the module-global `app.state` inside server.py helpers. Returns None
    when the mutation may proceed.
    """
    if is_read_only():
        return read_only_403()
    broker = str(getattr(state, "broker", "IB") or "IB").upper()
    if broker != "IB":
        from xenon.execution import futu_scope

        operation = op or _op_for_kind(kind)
        supported = broker_capabilities.CAPABILITIES.get("FUTU", {}).get(operation or "", False)
        if broker == "FUTU" and supported and futu_scope.parse_account_env(account_env) is None:
            return _futu_scope_required()  # a never-supported op keeps its 403 below
        denial = broker_capability_denial(broker, operation, kind, account_env)
        if denial is None and broker == "FUTU":
            denial = _futu_change_pending_denial(account_env, kind)
        return denial
    _check_mode_verified(state)
    return None


class ReadOnlyDenied(Exception):
    """Signal: request refused because XENON_READ_ONLY=1.

    Raised by `require_writable` route dependencies; the app-level
    exception handler in server.py converts it to the `read_only_403`
    JSONResponse so the refusal keeps the top-level `reason_code` shape."""


def require_writable() -> None:
    """Route dependency: refuse writes under XENON_READ_ONLY=1 early.

    Declared via `dependencies=[Depends(require_writable)]` — FastAPI
    solves route-level dependencies before sibling param dependencies
    (scope resolution) and request-body validation, so a `dev.sh live`
    session gets 403 READ_ONLY_MODE ahead of any 4xx/5xx those would
    produce.
    """
    if is_read_only():
        raise ReadOnlyDenied()


def get_account_scope(request: Request) -> AccountScope:
    """FastAPI dependency: resolve the current broker account scope from app.state.

    Populated by the server lifespan guard. Raises ValueError (→ 500) if the
    lifespan didn't run — production callers should also depend on
    `require_mode_verified` which produces the operator-friendly 503.
    """
    return resolve_from_app_state(request.app.state)


def _futu_scope_from_db() -> AccountScope | None:
    """Last-synced FUTU scope from Postgres (DB-first fallback when OpenD is down).

    Reads the most recent persisted Futu rows so the read path (orders / blotter /
    journal / performance) still serves cached data without a live OpenD connect.
    Returns None only when no Futu data has ever been synced.
    """
    from sqlalchemy import desc, select

    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import futu_orders, futu_trades

    engine = get_sync_engine()
    try:
        with engine.connect() as conn:
            for table, order_col in ((futu_orders, "updated_at"), (futu_trades, "filled_at")):
                row = conn.execute(
                    select(table.c.account_env, table.c.broker_account)
                    .where(table.c.broker == "FUTU")
                    .order_by(desc(table.c[order_col]))
                    .limit(1)
                ).first()
                if row is not None:
                    return AccountScope(broker="FUTU", account_env=row[0], broker_account=row[1])
    except Exception:  # pragma: no cover - DB unreachable too; let caller 503
        return None
    return None


def get_broker_scope(request: Request, broker: str | None = None) -> AccountScope:
    """Broker-aware scope dep for read routes (orders, blotter, journal, performance).

    `?broker=IB` (default) resolves the live IB scope from app.state (env fallback
    when the Gateway handshake is pending). `?broker=FUTU` returns the configured
    Futu trade scope when one is set; without it, it resolves from the
    lazily-connected FutuClient's matched account; if OpenD is unreachable it falls
    back to the last-synced FUTU scope in Postgres so the DB-first read path keeps
    serving cached orders/blotter/journal. Only 503s when OpenD is down AND no Futu
    data has ever been synced.
    """
    b = (broker or "IB").upper()
    if b == "IB":
        try:
            return resolve_from_app_state(request.app.state)
        except ValueError:
            # IB Gateway API handshake failed (TCP open, auth not accepted —
            # commonly "needs 2FA on IBKR mobile" or "client IP not in trusted
            # list"). app.state.account stays empty. The read path is
            # Postgres-only, so degrade gracefully via env vars (dev.sh
            # exports XENON_BROKER_ACCOUNT + XENON_TRADING_MODE before boot).
            from xenon.execution.account_scope import resolve_from_env

            try:
                return resolve_from_env()
            except ValueError as exc:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"IB account scope unresolved (Gateway handshake pending and env fallback unavailable): {exc}"
                    ),
                )
    if b != "FUTU":
        raise HTTPException(status_code=400, detail=f"Unknown broker: {broker!r}")

    # FUTU read path — NON-BLOCKING. Never initiate a connect here: the futu SDK
    # retries a refused OpenD indefinitely (conn=0(1), 0(2), ...) without raising,
    # which would hang the read request and wedge the event loop. Use the live
    # matched account only if the client is ALREADY connected; otherwise serve the
    # last-synced scope from Postgres (DB-first). The connect happens on the write
    # path (orders_refresh / blotter sync), bounded + off the event loop.
    # Importing here avoids a circular import with server.py / routes.
    return futu_request_scope(request)


def futu_request_scope(request: Request) -> AccountScope:
    """FUTU read scope of the request (§11): `account_env` from the query
    (missing → default, invalid → 400), then the configured account of that
    env. An env without an account → the not-configured scope (empty reads).
    Only when no Futu account is configured at all does the legacy scope
    apply (the connected client's matched account, else the last-synced one),
    and only when it is of the requested env."""
    from xenon.api.routes import futu as futu_routes
    from xenon.execution import futu_scope
    from xenon.execution.account_scope import env_from_trd_env

    env = futu_read_env(request)
    configured, _ = futu_scope.configured_scope(env)
    if configured is not None:
        return configured
    not_configured = AccountScope(broker="FUTU", account_env=env, broker_account=FUTU_NOT_CONFIGURED_ACCOUNT)
    if futu_scope.configured_scopes():
        return not_configured

    legacy: AccountScope | None = None
    client = futu_routes._get_futu_client()
    if client.is_connected():
        trd_env = client.trd_env_of_matched_account()
        acc_id = getattr(client, "_acc_id", None)
        if trd_env is not None and acc_id is not None:
            try:
                legacy = AccountScope(broker="FUTU", account_env=env_from_trd_env(trd_env), broker_account=str(acc_id))
            except ValueError as exc:
                raise HTTPException(status_code=502, detail=str(exc))
    if legacy is None:
        # Not connected (or not matched yet) → DB-first fallback, no blocking connect.
        legacy = _futu_scope_from_db()
    return legacy if legacy is not None and legacy.account_env == env else not_configured


# Backwards-compatible alias (the performance route imported the old name).
get_performance_scope = get_broker_scope
