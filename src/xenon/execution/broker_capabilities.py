"""Per-broker, per-operation order capability.

`CAPABILITIES` is what each broker can support at all. IB supports every
operation (unchanged behaviour). FUTU supports single-leg place, cancel and
modify only; replace, combo and wizard are never allowed.

`capability()` adds the runtime gate: a FUTU operation is allowed only when
`XENON_FUTU_TRADING=1` (exactly) AND `futu_scope.configured_scope()` is
valid. The live OpenD account check (`futu_scope.verify_live`) runs before
each send, not here. Every denial uses `READ_ONLY_BROKER`, the existing
reason code the web already maps.
"""

from __future__ import annotations

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


def evaluate(broker: str | None, op: str | None) -> tuple[bool, str | None, str]:
    """Return `(ok, reason_code, cause)`; `cause` is operator text for a denial."""
    name = str(broker or "").upper()
    table = CAPABILITIES.get(name)
    if table is None:
        return False, _DENIED, f"unknown broker {broker!r}"
    if not table.get(op or "", False):
        return False, _DENIED, f"{name} does not support {op!r}"
    if name == "FUTU":
        if os.environ.get("XENON_FUTU_TRADING") != "1":
            return False, _DENIED, "XENON_FUTU_TRADING is not 1"
        scope, reason = futu_scope.configured_scope()
        if scope is None:
            return False, _DENIED, f"no valid Futu trade scope ({reason})"
    return True, None, ""


def capability(broker: str | None, op: str | None) -> tuple[bool, str | None]:
    """Return `(ok, reason_code)`; `reason_code` is None when allowed."""
    ok, reason_code, _ = evaluate(broker, op)
    return ok, reason_code
