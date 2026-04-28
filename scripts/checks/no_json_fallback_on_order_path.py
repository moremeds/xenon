"""Layer 2a guard — fail new silent JSON fallbacks on the order path.

Background
----------
Postgres is the runtime source of truth for portfolio, orders, NAV, vcg.
Any `data/*.json` read on a runtime/order path is a regression vector
(see [Postgres read-side gap] memory + PR #61). PR #61 removed the
fallback in `web/app/api/orders/place/route.ts` but four sibling routes
still read `data/orders.json` and need the same migration.

This guard is a ratchet:
- The existing legacy reads are listed in `_ALLOWLIST` so the build does
  not break today.
- Any *new* file under the scanned roots that contains the forbidden
  pattern fails the check.
- Adding a file to the allowlist is a deliberate, reviewable act.

Forbidden patterns (TS/JS):
- `readDataFile("data/...json")`
- `readFile(... "data/....json" ...)` against any `data/*.json`
- `JSON.parse(... readFile ... data/*.json ...)`

Forbidden patterns (Python):
- `json.load*(... data/*.json ...)`
- `Path("data/orders.json").read_text()` (and similar)

Reference: docs/plans/2026-04-28-order-path-regression-prevention.md §Layer 2.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Roots scanned for forbidden JSON fallbacks.
_SCAN_ROOTS = (
    "web/app/api",
    "src/xenon/api",
)

# Files that currently contain forbidden patterns. Each entry is technical
# debt that should be migrated to FastAPI/Postgres reads. Do NOT add to
# this list without an open ticket and a target date for removal.
#
# Last verified: 2026-04-29 (commit 15be00e9 + a7bea3ad on master).
_ALLOWLIST = frozenset(
    {
        # Legacy: still reads data/orders.json. Migrate alongside the
        # `place/route.ts` pattern from PR #61.
        "web/app/api/orders/cancel/route.ts",
        "web/app/api/orders/modify/route.ts",
        "web/app/api/orders/route.ts",
    }
)

# Patterns considered violations. Each pattern is a regex matched against
# every line of every scanned file.
_VIOLATION_PATTERNS = (
    # TS / JS — readDataFile against any data/*.json
    re.compile(r'readDataFile\s*\(\s*"data/[^"]*\.json"'),
    # TS / JS — fs/promises readFile against data/*.json
    re.compile(r'readFile\s*\([^)]*"data/[^"]*\.json"'),
    # Python — json.load{,s} on a Path("data/...json") read
    re.compile(r'json\.loads?\s*\([^)]*"data/[^"]*\.json"'),
    # Python — Path("data/...json").read_text() / .read_bytes()
    re.compile(r'Path\s*\(\s*"data/[^"]*\.json"\s*\)\s*\.read_'),
)

# File extensions to scan.
_EXTENSIONS = (".ts", ".tsx", ".js", ".mjs", ".py")


def _iter_candidate_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for ext in _EXTENSIONS:
        files.extend(root.rglob(f"*{ext}"))
    # Skip node_modules, __pycache__, build artifacts.
    return [
        p for p in files if "node_modules" not in p.parts and "__pycache__" not in p.parts and ".next" not in p.parts
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-root", default=".", type=Path)
    ap.add_argument(
        "--show-allowlist",
        action="store_true",
        help="Print the allowlist and exit. Use this to audit known debt.",
    )
    args = ap.parse_args()

    if args.show_allowlist:
        print("Allowlisted files (legacy JSON-fallback debt):")
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
            if rel in _ALLOWLIST:
                continue
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(lines, start=1):
                for pattern in _VIOLATION_PATTERNS:
                    if pattern.search(line):
                        violations.append((rel, lineno, line.strip()))
                        break

    if not violations:
        print("OK — no new JSON fallbacks on order/migrated routes.")
        return 0

    print("FAIL — new JSON-fallback read detected on order/migrated route.", file=sys.stderr)
    print("", file=sys.stderr)
    print("These files reintroduce a regression pattern PR #61 removed.", file=sys.stderr)
    print("Postgres is the runtime source of truth (CLAUDE.md §Runtime", file=sys.stderr)
    print("Data Read Paths). Use xenonFetch / scoped queries instead.", file=sys.stderr)
    print("", file=sys.stderr)
    for rel, lineno, line in violations:
        print(f"  {rel}:{lineno}: {line}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "If this is genuinely intentional legacy debt, edit the _ALLOWLIST",
        file=sys.stderr,
    )
    print(
        "in scripts/checks/no_json_fallback_on_order_path.py and explain why.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
