#!/usr/bin/env python3
"""CI guard: forbid `ib_insync` references outside historical archives.

Background: PR #81 migrated from the unmaintained `ib_insync` (last release
July 2023) to the actively-maintained community fork `ib_async`. This guard
prevents accidental reintroduction of the old import in active code, tests,
or current documentation.

The guard scans the repo (excluding archives, build dirs, transient agent
working directories, and intentionally-historical files) for the literal
strings `ib_insync` or `ib-insync`. Any match fails the build with a
human-readable diff summary.

Run locally:
    uv run python scripts/checks/no_ib_insync.py

Allowlist: see ALLOWLIST_PATHS below — limited to historical / archive
paths where rewriting the past would distort the record.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Forbidden patterns. Word-boundary anchored so we don't false-match on e.g.
# `feedback_ib_insync_in_fastapi.md` (we already renamed those).
FORBIDDEN = re.compile(r"\bib[_-]insync\b")

# Directories we never scan: build artifacts, dependencies, transient agent
# state, and historical archives where the past use of ib_insync is part of
# the record.
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
    "archive",  # docs/plans/archive, docs/superpowers/archive, etc.
    "_archive",  # docs/handovers/_archive, docs/note/_archive — archived historical docs
    "tasks",  # tasks/lessons.md, tasks/PROGRESS.md — historical context
    "output",  # test-output captures
}

# File extensions to scan. Skip binaries.
INCLUDE_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".md",
    ".mdx",
    ".html",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".sh",
    ".cfg",
    ".ini",
}

# Specific files that are intentionally historical and exempted.
ALLOWLIST_PATHS: set[Path] = {
    REPO_ROOT / "docs/plans/2026-04-28-postgres-migration-completion-IMPL.md",
    REPO_ROOT / "docs/plans/2026-04-29-postgres-migration-review-fixes.md",
    REPO_ROOT / "scripts/checks/no_ib_insync.py",  # this file references the term itself
    REPO_ROOT / ".github/workflows/ci.yml",  # CI step name documents what this guard checks for
    REPO_ROOT / "CHANGELOG.md",  # release notes document migrations away from deprecated symbols
}


def is_excluded(path: Path) -> bool:
    parts = path.relative_to(REPO_ROOT).parts
    return any(part in EXCLUDE_DIRS for part in parts)


def main() -> int:
    violations: list[tuple[Path, int, str]] = []

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
                rel = path.relative_to(REPO_ROOT)
                violations.append((rel, lineno, line.rstrip()))

    if not violations:
        print("OK: no ib_insync references in active code or current docs.")
        return 0

    print("FAIL: ib_insync references found in active code or current docs.")
    print(f"      Migrate these to ib_async (PR #81 migrated the project on 2026-05-03).")
    print()
    print(f"{len(violations)} violation(s):")
    for rel, lineno, line in violations:
        snippet = line.strip()[:120]
        print(f"  {rel}:{lineno}: {snippet}")
    print()
    print("If a reference is genuinely historical (archived plan, etc.), add it")
    print("to ALLOWLIST_PATHS in scripts/checks/no_ib_insync.py.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
