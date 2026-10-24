"""Per-broker, per-operation order capability.

`CAPABILITIES` is what each broker can support at all. IB supports every
operation (unchanged behaviour). FUTU supports single-leg place, cancel and
modify only; replace, combo and wizard are never allowed.

`capability()` adds the runtime gate: a FUTU operation is allowed only for
the request scope's environment, when that environment is in
`XENON_FUTU_TRADING` (a comma set over `simulate`, `real`; any other value,
including the old `1`, means off for both, with one ERROR log) AND that
environment has a configured account (`futu_scope.configured_scope`). The live OpenD account check (`futu_scope.verify_live`) runs before
each send, not here. Every denial uses `READ_ONLY_BROKER`, the existing
reason code the web already maps.
"""

from __future__ import annotations

import logging
import os

from xenon.execution import futu_scope
from xenon.execution.preflight import ReasonCode

OPERATIONS = ("place_single", "cancel", "modify", "replace", "combo", "wizard")

CAPABILITIES: dict[str, dict[str, bool]] = {
    "IB": dict.fromkeys(OPERATIONS, True),
    "FUTU": {
        "place_single": True,
        "cancel": True,
        "modify": True,
        "replace": False,
        "combo": False,
        "wizard": False,
    },
}

_DENIED = ReasonCode.READ_ONLY_BROKER.value

log = logging.getLogger(__name__)

TRADING_FLAG = "XENON_FUTU_TRADING"
_FLAG_TO_ENV = {"simulate": "paper", "real": "live"}
_bad_flags_logged: set[str] = set()


def futu_trading_envs() -> frozenset[str]:
    """Account envs with Futu trading on: `simulate` → paper, `real` → live.
    Unset or empty → none. Any other token (including the old `1`) → none,
    with one ERROR log per value."""
    raw = os.environ.get(TRADING_FLAG, "")
    if not raw.strip():
        return frozenset()
    tokens = [t.strip().lower() for t in raw.split(",")]
    if any(t not in _FLAG_TO_ENV for t in tokens):
        if raw not in _bad_flags_logged:
            _bad_flags_logged.add(raw)
            log.error("%s=%r is not a set over {simulate, real}: Futu trading is off", TRADING_FLAG, raw)
        return frozenset()
    return frozenset(_FLAG_TO_ENV[t] for t in tokens)


def evaluate(broker: str | None, op: str | None, account_env: str | None = None) -> tuple[bool, str | None, str]:
    """Return `(ok, reason_code, cause)`; `cause` is operator text for a denial.
    `account_env` is the FUTU request scope's environment (ignored for IB)."""
    name = str(broker or "").upper()
    table = CAPABILITIES.get(name)
    if table is None:
        return False, _DENIED, f"unknown broker {broker!r}"
    if not table.get(op or "", False):
        return False, _DENIED, f"{name} does not support {op!r}"
    if name == "FUTU":
        env = futu_scope.parse_account_env(account_env)
        if env is None:
            return False, _DENIED, "no Futu account_env on the request"
        if env not in futu_trading_envs():
            return False, _DENIED, f"{TRADING_FLAG} does not enable {env}"
        scope, reason = futu_scope.configured_scope(env)
        if scope is None:
            return False, _DENIED, f"no Futu account configured for {env} ({reason})"
    return True, None, ""


def capability(broker: str | None, op: str | None, account_env: str | None = None) -> tuple[bool, str | None]:
    """Return `(ok, reason_code)`; `reason_code` is None when allowed."""
    ok, reason_code, _ = evaluate(broker, op, account_env)
    return ok, reason_code
