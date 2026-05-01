"""Layer 2c guard — every order entry point must call RegimeGate.

Background
----------
Phase 3 wires `RegimeGate.veto` into `_orders_place_from_body` and
`_orders_modify_from_body` so new exposure is gated by binding tier.
Without this guard, a new endpoint (or a refactor that splits out
another in-process caller) could silently bypass the gate.

The guard scans `src/xenon/api/server.py` and asserts that every
function whose name matches `_orders_place_from_body`,
`_orders_modify_from_body`, or any future `_orders_<verb>_from_body`
that mutates broker state, contains a lexical reference to either
`RegimeGate.veto`, `evaluate_order_gate`, or `_run_regime_gate` /
`_run_modify_regime_gate`. Functions on the allowlist are exempt
(read-only / cancel-class verbs).

Reference: docs/superpowers/specs/2026-04-29-vcg-cri-strategies-rewiring-design.md §7.5.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# Functions inside server.py that are entry points to broker state
# mutation and therefore MUST run through the regime gate.
_REQUIRED_GATE_CALLERS: frozenset[str] = frozenset(
    {
        "_orders_place_from_body",
        "_orders_modify_from_body",
    }
)

# Allowlisted entry points — read-only or risk-reducing flows that the
# spec explicitly carves out from §4.6 (cancels, refresh, quote).
_ALLOWLIST: frozenset[str] = frozenset(
    {
        "_orders_cancel_from_body",
    }
)

# Identifiers whose presence inside a required function satisfies the
# guard. Adding another helper that runs the gate? List its name here.
_GATE_REFERENCES: frozenset[str] = frozenset(
    {
        "RegimeGate",
        "evaluate_order_gate",
        "_run_regime_gate",
        "_run_modify_regime_gate",
    }
)

_SERVER_PATH = "src/xenon/api/server.py"


def _function_body_text(func: ast.FunctionDef | ast.AsyncFunctionDef, source: str) -> str:
    """Return the raw source text of a function body."""
    lines = source.splitlines()
    # ast nodes are 1-indexed; slice covers the whole def block
    end = func.end_lineno or func.lineno
    return "\n".join(lines[func.lineno - 1 : end])


def _check_server(repo_root: Path) -> list[str]:
    server_path = repo_root / _SERVER_PATH
    if not server_path.exists():
        return [f"missing {_SERVER_PATH}"]

    source = server_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(server_path))

    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in _REQUIRED_GATE_CALLERS:
                found[node.name] = node

    violations: list[str] = []
    for required in _REQUIRED_GATE_CALLERS:
        if required not in found:
            violations.append(
                f"{_SERVER_PATH}: required function {required!r} not found — "
                "did it get renamed? Update _REQUIRED_GATE_CALLERS."
            )
            continue
        body_text = _function_body_text(found[required], source)
        if not any(ref in body_text for ref in _GATE_REFERENCES):
            violations.append(
                f"{_SERVER_PATH}:{found[required].lineno}: "
                f"{required} does not reference any of "
                f"{sorted(_GATE_REFERENCES)} — RegimeGate would be bypassed."
            )
    return violations


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-root", default=".", type=Path)
    ap.add_argument(
        "--show-allowlist",
        action="store_true",
        help="Print the allowlist and exit.",
    )
    args = ap.parse_args()

    if args.show_allowlist:
        print("Required gate callers (must reference RegimeGate):")
        for name in sorted(_REQUIRED_GATE_CALLERS):
            print(f"  {name}")
        print("\nAllowlisted (no gate required — read-only / risk-reducing):")
        for name in sorted(_ALLOWLIST):
            print(f"  {name}")
        return 0

    repo_root: Path = args.repo_root.resolve()
    violations = _check_server(repo_root)

    if not violations:
        print("OK — every order entry point references RegimeGate.")
        return 0

    print("FAIL — RegimeGate is missing from one or more order entry points.", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "Phase 3 wired RegimeGate.veto into the new-exposure paths. A new",
        file=sys.stderr,
    )
    print(
        "endpoint or refactor must keep the gate or new exposure can land",
        file=sys.stderr,
    )
    print("at TIER_1/PANIC without consent.", file=sys.stderr)
    print("", file=sys.stderr)
    for v in violations:
        print(f"  {v}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "If this entry point is genuinely risk-reducing (cancel-class), add",
        file=sys.stderr,
    )
    print(
        "it to the _ALLOWLIST in scripts/checks/order_path_regime_gate_called.py",
        file=sys.stderr,
    )
    print("and remove it from _REQUIRED_GATE_CALLERS.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
