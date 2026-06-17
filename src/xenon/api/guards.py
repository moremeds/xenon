"""FastAPI-tier helpers for the trading-mode switch.

Lives separate from `xenon.api.trading_mode` so the config layer (used by
`xenon.clients.ib_client` for port derivation) does NOT need to import
fastapi. Only routes/lifespan code touches this module.
"""

from __future__ import annotations

import os

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from xenon.api import trading_mode
from xenon.execution.account_scope import AccountScope, resolve_from_app_state


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


def require_mode_verified(request: Request) -> None:
    """FastAPI dependency: reject order-mutating requests when mode is unverified.

    Reads `app.state.{trading_mode,account,mode_verified}` populated by the
    server lifespan guard. Returns 503 with both the declared mode and the
    observed account in the body so the operator can fix the mismatch
    (edit .env or relog Gateway). The 503 detail is auth-gated (only routes
    behind auth call this), so leaking the raw account here is fine.
    """
    state = request.app.state
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
    when the Gateway handshake is pending). `?broker=FUTU` resolves from the
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
    # Importing here avoids a circular import with server.py.
    from xenon.api import server as _server
    from xenon.execution.account_scope import env_from_trd_env

    client = _server._get_futu_client()
    if client.is_connected():
        trd_env = client.trd_env_of_matched_account()
        acc_id = getattr(client, "_acc_id", None)
        if trd_env is not None and acc_id is not None:
            try:
                env = env_from_trd_env(trd_env)
            except ValueError as exc:
                raise HTTPException(status_code=502, detail=str(exc))
            return AccountScope(broker="FUTU", account_env=env, broker_account=str(acc_id))
    # Not connected (or not matched yet) → DB-first fallback, no blocking connect.
    db_scope = _futu_scope_from_db()
    if db_scope is not None:
        return db_scope
    raise HTTPException(status_code=503, detail="Futu not connected and no synced Futu data yet")


# Backwards-compatible alias (the performance route imported the old name).
get_performance_scope = get_broker_scope
