"""Layer 2b guard — restrict who can place IB orders.

Background
----------
Order placement runs through `xenon.execution.ib_place_order` (CLI:
`xenon-ib-place-order`). FastAPI invokes it as a subprocess from
`src/xenon/api/server.py` *only after* `_run_preflight()` and
`_validate_non_combo_quote()` have approved the request. Any other
caller — direct Python import or independent CLI invocation —
sidesteps Gate 4, the preflight, the quote gate, and idempotency.

This is exactly the [In-process route bypass] regression class that has
already burned the repo twice (#34 quote_token, #47 audit gating).

This guard fails the build if any non-allowlisted file references the
placement entry point.

Forbidden references:
- `from xenon.execution.ib_place_order import ...`
- `from xenon.execution import ib_place_order`
- `import xenon.execution.ib_place_order`
- The string `xenon-ib-place-order` (the console script name) outside
  of declaration/documentation files.

Reference: docs/plans/2026-04-28-order-path-regression-prevention.md §Layer 2.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Files allowed to import or invoke ib_place_order. Adding to this list
# is a deliberate trust extension — review the implications against the
# preflight / quote-gate / idempotency contracts before doing so.
_ALLOWLIST = frozenset(
    {
        # The module itself (self-references in docstrings, __main__).
        "src/xenon/execution/ib_place_order.py",
        # The single legitimate runtime caller — invokes via subprocess
        # after preflight + quote-gate have run.
        "src/xenon/api/server.py",
        # Console-script declaration.
        "pyproject.toml",
        # Static analysis — the guard itself names the symbol.
        "scripts/checks/order_path_caller_allowlist.py",
        # CLIENT_IDS dict (declarative — not an actual import or invocation).
        "src/xenon/clients/ib_client.py",
    }
)

# Roots scanned. We scan widely so anything new in the repo gets caught.
_SCAN_ROOTS = ("src", "scripts", "web")

# Patterns that signal an import or invocation of the placement entry.
_VIOLATION_PATTERNS = (
    re.compile(r"^\s*from\s+xenon\.execution\.ib_place_order\s+import\b"),
    re.compile(r"^\s*from\s+xenon\.execution\s+import\b[^#\n]*\bib_place_order\b"),
    re.compile(r"^\s*import\s+xenon\.execution\.ib_place_order\b"),
    re.compile(r"\bxenon-ib-place-order\b"),
)

_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".mjs", ".sh", ".toml", ".yml", ".yaml")

# Test files are exempt — by convention these import production code to
# verify it. The guard's threat model is in-process production callers.
_TEST_PATH_HINTS = ("/tests/", "/test_", "/__tests__/")
_TEST_FILE_PREFIX = "test_"


def _is_test_file(rel_path: str) -> bool:
    if any(hint in f"/{rel_path}" for hint in _TEST_PATH_HINTS):
        return True
    name = rel_path.rsplit("/", 1)[-1]
    return name.startswith(_TEST_FILE_PREFIX) or name.endswith("_test.py")


def _iter_candidate_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for ext in _EXTENSIONS:
        files.extend(root.rglob(f"*{ext}"))
    return [
        p
        for p in files
        if "node_modules" not in p.parts
        and "__pycache__" not in p.parts
        and ".next" not in p.parts
        and ".venv" not in p.parts
    ]


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
        print("Allowlisted files (may import/invoke ib_place_order):")
        for path in sorted(_ALLOWLIST):
            print(f"  {path}")
        return 0

    repo_root: Path = args.repo_root.resolve()
    violations: list[tuple[str, int, str]] = []

    for scan_root_rel in _SCAN_ROOTS:
        scan_root = repo_root / scan_root_rel
        if not scan_root.exists():
            continue
        for file_path in _iter_candidate_files(scan_root):
            rel = file_path.relative_to(repo_root).as_posix()
            if rel in _ALLOWLIST or _is_test_file(rel):
                continue
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(lines, start=1):
                # Skip comment lines — Python `#`, JS/TS `//`, or YAML/TOML `#`.
                stripped = line.lstrip()
                if stripped.startswith(("#", "//")):
                    continue
                for pattern in _VIOLATION_PATTERNS:
                    if pattern.search(line):
                        violations.append((rel, lineno, line.strip()))
                        break

    if not violations:
        print("OK — no unauthorized callers of ib_place_order.")
        return 0

    print(
        "FAIL — unauthorized reference to ib_place_order / xenon-ib-place-order.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print(
        "Order placement must go through src/xenon/api/server.py after",
        file=sys.stderr,
    )
    print(
        "_run_preflight() + _validate_non_combo_quote() have approved the",
        file=sys.stderr,
    )
    print(
        "request. Direct imports or CLI invocations bypass Gate 4 and have",
        file=sys.stderr,
    )
    print("caused regressions twice (PRs #34, #47).", file=sys.stderr)
    print("", file=sys.stderr)
    for rel, lineno, line in violations:
        print(f"  {rel}:{lineno}: {line}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "If this caller is genuinely required, edit the _ALLOWLIST in",
        file=sys.stderr,
    )
    print(
        "scripts/checks/order_path_caller_allowlist.py and document why.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
