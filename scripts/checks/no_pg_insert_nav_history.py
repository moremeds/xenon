#!/usr/bin/env python3
"""CI guard: only ``portfolio_loader.py`` may ``pg_insert(nav_history)``.

Pass-2 T4 / writer unification — Codex found a 4th nav_history writer
(``xenon.db.queries.portfolio.upsert_nav``) that Pass-1's self-review missed.
This guard prevents the same drift in the future.

Uses Python's ``tokenize`` module to skip comments + string literals, so
docstrings that mention the symbol name don't trip the guard.
"""

from __future__ import annotations

import io
import sys
import tokenize
from pathlib import Path

# Production code paths that own the surface. Test fixtures often build
# seed rows for nav_history directly via SQL — they're allowed under
# ``scripts/tests/`` because exercising the helper path adds no signal
# (the helper itself is what they're testing).
ALLOW_FILES: set[str] = {
    "src/xenon/utils/portfolio_loader.py",
}

ALLOW_PREFIXES: tuple[str, ...] = (
    "scripts/tests/",
    "src/xenon/api/tests/",
    "src/xenon/db/tests/",
)

EXCLUDE_DIRS = (
    ".venv",
    "__pycache__",
    ".worktrees",
    "node_modules",
    "data",
    ".pytest_cache",
    "scripts/checks",  # this file references the pattern by name
)


def _scan_tokens(path: Path) -> list[int]:
    """Return line numbers where bare ``pg_insert(nav_history)`` appears as code."""
    hits: list[int] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return hits
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenizeError, IndentationError):
        return hits

    # State machine: detect `pg_insert(nav_history)` or `insert(nav_history)`.
    # Token sequence is NAME(pg_insert), OP("("), NAME(nav_history), OP(")")
    for i in range(len(tokens) - 3):
        t0, t1, t2, t3 = tokens[i], tokens[i + 1], tokens[i + 2], tokens[i + 3]
        if t0.type != tokenize.NAME or t0.string not in {"pg_insert", "insert"}:
            continue
        if t1.type != tokenize.OP or t1.string != "(":
            continue
        if t2.type != tokenize.NAME or t2.string != "nav_history":
            continue
        if t3.type != tokenize.OP or t3.string != ")":
            continue
        hits.append(t0.start[0])
    return hits


def find_offenders(root: Path) -> list[str]:
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).as_posix()
        if any(f"/{d}/" in f"/{rel}/" or rel.startswith(f"{d}/") for d in EXCLUDE_DIRS):
            continue
        if rel in ALLOW_FILES:
            continue
        if any(rel.startswith(p) for p in ALLOW_PREFIXES):
            continue
        for line_no in _scan_tokens(path):
            offenders.append(f"{rel}:{line_no}")
    return offenders


def main() -> int:
    if "--show-allowlist" in sys.argv:
        print("# Production files that may pg_insert(nav_history):")
        for rel in sorted(ALLOW_FILES):
            print(rel)
        print("\n# Test directories exempted (seed rows for fixtures):")
        for p in ALLOW_PREFIXES:
            print(p)
        return 0
    root = Path(__file__).resolve().parents[2]
    offenders = find_offenders(root)
    if offenders:
        print(
            "FAIL: pg_insert(nav_history) / insert(nav_history) found outside the allowlist:",
            file=sys.stderr,
        )
        for o in offenders:
            print(f"  - {o}", file=sys.stderr)
        print(
            "\nAll nav_history writes must funnel through "
            "xenon.utils.portfolio_loader.upsert_nav_sync / upsert_nav_async.",
            file=sys.stderr,
        )
        return 1
    print("OK: only portfolio_loader.py uses pg_insert(nav_history).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
