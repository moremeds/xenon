"""Single source of truth for paper-vs-live trading mode.

Driven by the `XENON_TRADING_MODE` env var. Owned constants:
- MODE              — "paper" | "live"
- EXPECTED_PORT     — 4002 (paper) | 4001 (live)
- EXPECTED_PREFIX   — "DU" (paper) | "U" (live, but not "DU")

`verify_account(account)` returns True iff the account string matches the
declared mode's prefix. Used by the startup guard (server.py lifespan) to
catch ".env says live but Gateway is logged in as paper" and vice versa.

Spec: docs/superpowers/specs/2026-04-25-paper-live-mode-switch-design.md
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request

# Load repo `.env` BEFORE reading XENON_TRADING_MODE, otherwise downstream
# imports (ib_client, ib_connection, ib_gateway) bind their port constants
# from a stale environment because they import this module before any other
# entry point has called load_dotenv(). MODE is bound at import time;
# changing XENON_TRADING_MODE requires a process restart.
try:
    from dotenv import load_dotenv

    _PROJECT_ROOT = Path(__file__).resolve().parents[3]
    load_dotenv(_PROJECT_ROOT / ".env")
except ImportError:
    pass


Mode = Literal["paper", "live"]

_DEFAULT_MODE: Mode = "paper"
_PORT_BY_MODE: dict[Mode, int] = {"paper": 4002, "live": 4001}
_PREFIX_BY_MODE: dict[Mode, str] = {"paper": "DU", "live": "U"}


def parse_mode(raw: str | None) -> Mode:
    """Parse and validate the mode env var. Defaults to 'paper' when unset/blank."""
    if raw is None:
        return _DEFAULT_MODE
    value = raw.strip().lower()
    if not value:
        return _DEFAULT_MODE
    if value not in _PORT_BY_MODE:
        raise ValueError(f"XENON_TRADING_MODE must be 'paper' or 'live', got {raw!r}")
    return value  # type: ignore[return-value]


MODE: Mode = parse_mode(os.environ.get("XENON_TRADING_MODE"))
EXPECTED_PORT: int = _PORT_BY_MODE[MODE]
EXPECTED_PREFIX: str = _PREFIX_BY_MODE[MODE]


def verify_account(account: str | None) -> bool:
    """True iff `account` matches the declared mode's prefix.

    Live mode rejects 'DU…' explicitly — a bare `startswith("U")` would
    accept paper accounts since they also start with U after the D.
    """
    if not account:
        return False
    if MODE == "paper":
        return account.startswith("DU")
    # live: starts with U but NOT DU
    return account.startswith("U") and not account.startswith("DU")


def mask_account(account: str | None) -> str:
    """Mask all but the last 4 chars of an account number for public display.

    Used by /health (auth-exempt path). Empty/short values are returned as-is
    so the caller can still distinguish "unknown" from a real account.
    """
    if not account:
        return ""
    if len(account) <= 4:
        return account
    return account[:2] + "***" + account[-4:]


def require_mode_verified(request: Request) -> None:
    """FastAPI dependency: reject order-mutating requests when mode is unverified.

    Reads `app.state.{trading_mode,account,mode_verified}` populated by the
    server lifespan guard. Returns 503 with both the declared mode and the
    observed account in the body so the operator can fix the mismatch
    (edit .env or relog Gateway). Lives here (not in server.py) so wizard
    routes can apply it without circular imports.
    """
    state = request.app.state
    if getattr(state, "mode_verified", False):
        return
    declared = getattr(state, "trading_mode", MODE)
    observed = getattr(state, "account", "")
    raise HTTPException(
        status_code=503,
        detail=(
            f"Trading mode mismatch: .env declares XENON_TRADING_MODE={declared!r} "
            f"but IB Gateway is logged in as account={observed!r} "
            f"(expected prefix {EXPECTED_PREFIX!r}). "
            f"Fix: align .env with the Gateway login and restart."
        ),
    )
