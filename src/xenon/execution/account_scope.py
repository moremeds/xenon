"""Broker account scope — durable identity for every execution/portfolio row.

Two resolution paths:
1. FastAPI: `resolve_from_app_state(app_state)` reads `app.state.{trading_mode, account}`.
2. Sync callers (ib_sync, ib_execute, rehydrate): `resolve_from_env()` reads
   `XENON_BROKER`, `XENON_TRADING_MODE` + `XENON_BROKER_ACCOUNT` env vars. The IB sync path
   should set `XENON_BROKER_ACCOUNT` from `managedAccounts()[0]` at connect time.

Both paths return a frozen AccountScope that gets stamped on every DB write.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class AccountScope:
    broker: Literal["IB", "FUTU"]
    account_env: Literal["paper", "live", "sim", "legacy_unknown"]
    broker_account: str

    def as_dict(self) -> dict[str, str]:
        return {
            "broker": self.broker,
            "account_env": self.account_env,
            "broker_account": self.broker_account,
        }


_MODE_TO_PREFIX: dict[str, str] = {"paper": "DU", "live": "U"}


def env_from_trd_env(trd_env: str) -> str:
    """Map FutuClient's matched_trd_env to xenon's account_env value.

    Spec §10 (perf-rebuild). REAL→"live", SIMULATE→"paper". Aligned with IB's
    convention so the unique-index on (broker, broker_account, date) cannot
    collide between the FUTU path and any historical sync that used MODE.
    Raises on unknown input — callers must validate via
    FutuClient.trd_env_of_matched_account() first.
    """
    if trd_env == "REAL":
        return "live"
    if trd_env == "SIMULATE":
        return "paper"
    raise ValueError(f"Unknown Futu trd_env: {trd_env!r}")


def resolve_from_env() -> AccountScope:
    """Build scope from environment variables. Used by sync IB callers only.

    Spec §10: FUTU scope cannot be resolved from env — use `persist_futu_nav`
    or `get_performance_scope` instead. The matched OpenD account is the only
    authoritative source for FUTU's env (not a global env var).
    """
    from xenon.api.trading_mode import MODE

    broker = os.environ.get("XENON_BROKER", "IB").strip().upper() or "IB"
    if broker == "FUTU":
        raise ValueError(
            "FUTU scope cannot be resolved from env vars — "
            "use persist_futu_nav or get_performance_scope (spec §10)."
        )
    if broker != "IB":
        raise ValueError(f"XENON_BROKER={broker!r} is not supported")
    account = os.environ.get("XENON_BROKER_ACCOUNT", "").strip()
    if not account:
        raise ValueError(
            "XENON_BROKER_ACCOUNT must be set (e.g. DU1234567). "
            "The IB sync path should set this from managedAccounts()[0]."
        )
    expected_prefix = _MODE_TO_PREFIX.get(MODE, "")
    if expected_prefix == "U":
        if not account.startswith("U") or account.startswith("DU"):
            raise ValueError(
                f"XENON_BROKER_ACCOUNT={account!r} does not match "
                f"XENON_TRADING_MODE={MODE!r} (expected prefix {expected_prefix!r}, "
                f"not 'DU') — mismatch"
            )
    elif expected_prefix and not account.startswith(expected_prefix):
        raise ValueError(
            f"XENON_BROKER_ACCOUNT={account!r} does not match "
            f"XENON_TRADING_MODE={MODE!r} (expected prefix {expected_prefix!r}) — mismatch"
        )
    return AccountScope(broker="IB", account_env=MODE, broker_account=account)


def resolve_from_app_state(app_state) -> AccountScope:
    """Build scope from FastAPI app.state. Used by async route handlers."""
    mode = getattr(app_state, "trading_mode", None)
    account = getattr(app_state, "account", None)
    broker = str(getattr(app_state, "broker", "IB") or "IB").upper()
    if not mode or not account:
        raise ValueError(
            "app.state.trading_mode and app.state.account must be set (populated by server lifespan guard)"
        )
    if broker not in ("IB", "FUTU"):
        raise ValueError(f"app.state.broker={broker!r} is not supported")
    return AccountScope(broker=broker, account_env=mode, broker_account=account)
