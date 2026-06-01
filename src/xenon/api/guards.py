"""FastAPI-tier helpers for the trading-mode switch.

Lives separate from `xenon.api.trading_mode` so the config layer (used by
`xenon.clients.ib_client` for port derivation) does NOT need to import
fastapi. Only routes/lifespan code touches this module.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from xenon.api import trading_mode
from xenon.execution.account_scope import AccountScope, resolve_from_app_state


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


def get_performance_scope(request: Request, broker: str | None = None) -> AccountScope:
    """Broker-aware scope dep for the performance route.

    `?broker=IB` (default) resolves the live IB scope from app.state.
    `?broker=FUTU` resolves from the lazily-connected FutuClient's matched
    account — connecting if necessary. Raises 503 when the Futu OpenD is
    unreachable so the UI can render a connect-prompt distinctly from a
    fatal error.
    """
    b = (broker or "IB").upper()
    if b == "IB":
        try:
            return resolve_from_app_state(request.app.state)
        except ValueError:
            # IB Gateway API handshake failed (TCP open, auth not accepted —
            # commonly "needs 2FA on IBKR mobile" or "client IP not in trusted
            # list"). app.state.account stays empty. The /performance read path
            # is Postgres-only, so degrade gracefully via env vars (dev.sh
            # exports XENON_BROKER_ACCOUNT + XENON_TRADING_MODE before boot).
            from xenon.execution.account_scope import resolve_from_env
            try:
                return resolve_from_env()
            except ValueError as exc:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"IB account scope unresolved (Gateway handshake pending and "
                        f"env fallback unavailable): {exc}"
                    ),
                )
    if b != "FUTU":
        raise HTTPException(status_code=400, detail=f"Unknown broker: {broker!r}")

    # FUTU path — connect on demand and read ground truth from the SDK.
    # Importing here avoids a circular import with server.py (which imports
    # this module during route registration).
    from xenon.api import server as _server
    from xenon.clients.futu_exceptions import FutuConnectionError, FutuError
    from xenon.execution.account_scope import env_from_trd_env

    client = _server._get_futu_client()
    if not client.is_connected():
        try:
            client.connect()
        except FutuConnectionError as exc:
            raise HTTPException(status_code=503, detail=f"Futu OpenD unreachable: {exc}")
        except FutuError as exc:
            raise HTTPException(status_code=502, detail=f"Futu error: {exc}")
    trd_env = client.trd_env_of_matched_account()
    if trd_env is None:
        raise HTTPException(status_code=503, detail="Futu account not matched yet")
    acc_id = getattr(client, "_acc_id", None)
    if acc_id is None:
        raise HTTPException(status_code=503, detail="Futu account id unavailable")
    try:
        env = env_from_trd_env(trd_env)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return AccountScope(broker="FUTU", account_env=env, broker_account=str(acc_id))
