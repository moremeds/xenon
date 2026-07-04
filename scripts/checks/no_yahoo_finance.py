#!/usr/bin/env python3
"""CI guard: forbid Yahoo Finance as a data source in active code.

Background: root CLAUDE.md mandates "Never use Yahoo Finance." Data flows
through IB, Futu OpenD, Unusual Whales, and IB Flex Query only. This guard
turns that standing rule into a CI invariant instead of prose an agent must
remember.

The guard scans active code files for `yfinance` imports/references and
`finance.yahoo.com` URLs. Markdown is not scanned — docs may legitimately
discuss the ban itself.

Run locally:
    uv run python scripts/checks/no_yahoo_finance.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN = re.compile(r"\byfinance\b|finance\.yahoo\.com")

EXCLUDE_DIRS = {
    ".git",
    ".venv",
    ".next",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".claude",
    ".worktrees",
    ".serena",
    ".pi",
    "archive",
    "_archive",
    "tasks",
    "output",
}

# Code files only — docs may discuss the ban itself.
INCLUDE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".sh", ".toml", ".yaml", ".yml"}

ALLOWLIST_PATHS: set[Path] = {
    REPO_ROOT / "scripts/checks/no_yahoo_finance.py",  # this file references the terms itself
    REPO_ROOT / ".github/workflows/ci.yml",  # CI step name documents the guard
}

# Pre-existing violations pinned at guard introduction (2026-07-04); tracked in
# docs/todo-backlog.md. Ratchet: a file may have AT MOST its baseline count of
# matches — adding a new Yahoo reference to an already-violating file fails CI.
# When you remove a call site, lower (or delete) its baseline in the same PR.
BASELINE_COUNTS: dict[Path, int] = {
    REPO_ROOT / "web/app/api/ticker/news/route.ts": 2,
    REPO_ROOT / "web/app/api/ticker/info/route.ts": 1,
    REPO_ROOT / "web/app/api/previous-close/route.ts": 1,
    REPO_ROOT / "src/xenon/reports/portfolio_performance.py": 1,
    REPO_ROOT / "scripts/research/spx_short_put_backtest.py": 3,
}


def is_excluded(path: Path) -> bool:
    parts = path.relative_to(REPO_ROOT).parts
    return any(part in EXCLUDE_DIRS for part in parts)


def main() -> int:
    violations: list[tuple[Path, int, str]] = []
    baseline_hits: dict[Path, list[tuple[int, str]]] = {p: [] for p in BASELINE_COUNTS}

    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in INCLUDE_SUFFIXES:
            continue
        if is_excluded(path):
            continue
        if path in ALLOWLIST_PATHS:
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for lineno, line in enumerate(text.splitlines(), 1):
            if FORBIDDEN.search(line):
                if path in BASELINE_COUNTS:
                    baseline_hits[path].append((lineno, line.rstrip()))
                else:
                    violations.append((path.relative_to(REPO_ROOT), lineno, line.rstrip()))

    ratchet_failures: list[str] = []
    for path, hits in baseline_hits.items():
        rel = path.relative_to(REPO_ROOT)
        allowed = BASELINE_COUNTS[path]
        if len(hits) > allowed:
            ratchet_failures.append(
                f"  {rel}: {len(hits)} Yahoo reference(s), baseline allows {allowed} — "
                f"new Yahoo usage added to an already-violating file"
            )
            for lineno, line in hits:
                ratchet_failures.append(f"    {rel}:{lineno}: {line.strip()[:120]}")
        elif len(hits) < allowed:
            print(
                f"NOTE: {rel} is below its baseline ({len(hits)} < {allowed}) — "
                f"lower BASELINE_COUNTS to lock in the cleanup."
            )

    if not violations and not ratchet_failures:
        print("OK: no new Yahoo Finance references in active code.")
        return 0

    print("FAIL: Yahoo Finance references found in active code.")
    print("      Root CLAUDE.md: 'Never use Yahoo Finance.' Use IB / Futu / UW instead.")
    print()
    if violations:
        print(f"{len(violations)} violation(s) in non-baseline files:")
        for rel, lineno, line in violations:
            print(f"  {rel}:{lineno}: {line.strip()[:120]}")
    if ratchet_failures:
        print("Ratchet failures (baseline exceeded):")
        for msg in ratchet_failures:
            print(msg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
