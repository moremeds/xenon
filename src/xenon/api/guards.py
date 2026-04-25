"""FastAPI-tier helpers for the trading-mode switch.

Lives separate from `xenon.api.trading_mode` so the config layer (used by
`xenon.clients.ib_client` for port derivation) does NOT need to import
fastapi. Only routes/lifespan code touches this module.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from xenon.api import trading_mode


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
