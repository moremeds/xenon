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
