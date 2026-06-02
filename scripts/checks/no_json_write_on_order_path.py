"""Layer 2b guard — fail new JSON writes to `data/*.json` on the order path.

Background
----------
Postgres is the runtime source of truth. The read-side guard
(`no_json_fallback_on_order_path.py`) blocks NEW JSON reads on the order
path; this guard blocks NEW JSON writes. Together they enforce the
"everything writes to DB first; website reads from DB" invariant.

Audit (2026-06-02, this guard's initial cut)
- src/xenon/api/server.py — writes `data/futu_portfolio{,.error}.json`
  (Futu sidecar; migration to PG in progress in a separate change)
- src/xenon/execution/futu_sync.py — writes `data/futu_portfolio.json`
  (same)
- src/xenon/execution/ib_reconcile.py — writes `data/reconciliation.json`
  (CLI audit artifact, not runtime state)
- web/app/api/ticker/{info,seasonality}/route.ts — writes
  `data/cache/<ticker>.json` (external-data caches)

Each of those sites is allowlisted explicitly. Anything ELSE that writes
JSON into `data/` from the scanned roots fails the check.

Forbidden patterns (TS/JS):
- `writeFile(... "data/....json" ...)`
- `writeFileSync(... "data/....json" ...)`
- `writeDataFile("data/....json", ...)`

Forbidden patterns (Python):
- `json.dump(...)` inside any function whose body also writes a
  `data/*.json` path (caught via co-occurrence)
- `Path("data/...json").write_text(...)`
- `open("data/...json", "w")` (and "wb")
- direct calls to `atomic_save(... "data/...json" ...)` or
  `_atomic_save(... "data/...json" ...)`

Reference: see docs/architecture/production-database-strategy.md
§ DB-first principle.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Roots scanned for forbidden JSON writes on the order/API path.
_SCAN_ROOTS = (
    "web/app/api",
    "web/lib/order",
    "src/xenon/api",
    "src/xenon/execution",
)

# Files that currently contain forbidden patterns. Each entry is either
# (a) a known sidecar being migrated separately, or (b) a non-runtime
# audit artifact. A new entry here is a real DB-first violation — fix the
# write, don't allowlist.
_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Futu sidecar — separate Futu→PG migration in flight (see CLAUDE.md
        # memory "Postgres read-side gap"). When that lands, drop these.
        "src/xenon/api/server.py",
        "src/xenon/execution/futu_sync.py",
        # IB reconcile CLI artifact — `data/reconciliation.json` is a
        # report, not runtime state. Manual CLI invocation only.
        "src/xenon/execution/ib_reconcile.py",
        # External-data caches under data/cache/<ticker>.json — explicitly
        # documented as cache, not source of truth.
        "web/app/api/ticker/info/route.ts",
        "web/app/api/ticker/seasonality/route.ts",
    }
)

# Patterns considered violations. Each is matched against every line of
# every scanned file (line-scoped, not function-scoped — pragmatic).
_VIOLATION_PATTERNS = (
    # TS / JS — writeFile{,Sync} pointed at a data/*.json path
    re.compile(r'(?:fs\.)?writeFile(?:Sync)?\s*\([^)]*"data/[^"]*\.json"'),
    # TS / JS — writeDataFile helper (mirrors readDataFile on the read side)
    re.compile(r'writeDataFile\s*\(\s*"data/[^"]*\.json"'),
    # Python — Path("data/...json").write_text / .write_bytes
    re.compile(r'Path\s*\(\s*"data/[^"]*\.json"\s*\)\s*\.write_'),
    # Python — open("data/...json", "w") / "wb" / "w+"
    re.compile(r'open\s*\(\s*"data/[^"]*\.json"\s*,\s*"w[b+]?"'),
    # Python — atomic_save("data/...json", ...) / _atomic_save(...)
    re.compile(r'_?atomic_save\s*\([^)]*"data/[^"]*\.json"'),
    # Python — json.dump(...) where the same line names a data/*.json file
    # (catches direct `with open("data/foo.json", "w") as f: json.dump(...)`).
    # The open() pattern above already catches the common case; this one
    # catches inline f-string targets and similar.
    re.compile(r'json\.dump\s*\([^)]*"data/[^"]*\.json"'),
)

# File extensions to scan.
_EXTENSIONS = (".ts", ".tsx", ".js", ".mjs", ".py")


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
        help="Print the allowlist and exit. Use this to audit known debt.",
    )
    args = ap.parse_args()

    if args.show_allowlist:
        print("Allowlisted files (sidecars + audit artifacts, NOT operational state):")
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
        print("OK — no new JSON writes to data/*.json on order/API path.")
        return 0

    print("FAIL — new JSON write to data/ detected on order/API path.", file=sys.stderr)
    print("", file=sys.stderr)
    print("Postgres is the runtime source of truth (CLAUDE.md §Runtime", file=sys.stderr)
    print("Data Read Paths + §Order-Path Guards). Operational state must", file=sys.stderr)
    print("be written to PG via orders_store / db.queries, not to JSON.", file=sys.stderr)
    print("", file=sys.stderr)
    for rel, lineno, line in violations:
        print(f"  {rel}:{lineno}: {line}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "If this is genuinely a non-operational sidecar (cache, audit",
        file=sys.stderr,
    )
    print(
        "artifact, in-flight migration), add the file to _ALLOWLIST in",
        file=sys.stderr,
    )
    print(
        "scripts/checks/no_json_write_on_order_path.py with a comment.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
