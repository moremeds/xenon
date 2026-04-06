#!/usr/bin/env python3
"""Run only pytest targets affected by changed Python files.

Default behavior:
- read changed files from `git diff --name-only <base>`
- keep only repo-local Python files
- map changed source files to matching tests under `scripts/tests/` and
  `scripts/trade_blotter/`
- execute pytest only for those resolved targets

The resolver is intentionally conservative:
- direct test-file edits run those tests
- `scripts/foo.py` prefers `scripts/tests/test_foo.py`
- nested modules also search for `test_*<stem>*.py`
- `conftest.py` changes expand to the whole sibling test tree
- if no Python files are changed, the runner exits 0 without invoking pytest
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOTS = (ROOT / "scripts" / "tests", ROOT / "scripts" / "trade_blotter")


def _rel_repo_path(path: str | Path) -> Path | None:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = (ROOT / candidate).resolve()
    try:
        rel = candidate.relative_to(ROOT)
    except ValueError:
        return None
    return rel


def is_repo_python_file(path: str | Path) -> bool:
    rel = _rel_repo_path(path)
    return rel is not None and rel.suffix == ".py"


def is_test_file(rel_path: Path) -> bool:
    return rel_path.name.startswith("test_") and (
        rel_path.parts[:2] == ("scripts", "tests")
        or rel_path.parts[:2] == ("scripts", "trade_blotter")
    )


def module_aliases(rel_path: Path) -> list[str]:
    stem = rel_path.with_suffix("")
    parts = stem.parts
    aliases = {stem.name}
    if parts and parts[0] == "scripts":
        aliases.add(".".join(parts[1:]))
        aliases.add(".".join(parts))
    else:
        aliases.add(".".join(parts))
    return [alias for alias in aliases if alias]


def _iter_test_files() -> list[Path]:
    files: list[Path] = []
    for root in TEST_ROOTS:
        if root.exists():
            files.extend(sorted(p for p in root.rglob("test_*.py") if p.is_file()))
    return files


def resolve_pytest_targets(changed_files: list[str | Path]) -> list[str]:
    targets: set[Path] = set()
    test_files = _iter_test_files()

    for changed in changed_files:
        rel = _rel_repo_path(changed)
        if rel is None or rel.suffix != ".py":
            continue

        abs_path = ROOT / rel
        if is_test_file(rel):
            targets.add(abs_path)
            continue

        if rel.name == "conftest.py":
            parent = abs_path.parent
            expanded = False
            for test_file in test_files:
                if parent in test_file.parents:
                    targets.add(test_file)
                    expanded = True
            if expanded:
                continue

        stem = rel.stem
        direct_candidates = [
            ROOT / "scripts" / "tests" / f"test_{stem}.py",
            ROOT / "scripts" / "trade_blotter" / f"test_{stem}.py",
        ]
        for candidate in direct_candidates:
            if candidate.exists():
                targets.add(candidate)

        for test_file in test_files:
            if stem in test_file.stem:
                targets.add(test_file)

        aliases = module_aliases(rel)
        import_patterns = [
            re.compile(rf"\bfrom\s+{re.escape(alias)}\s+import\b")
            for alias in aliases
        ] + [
            re.compile(rf"\bimport\s+{re.escape(alias)}(?:\b|$)")
            for alias in aliases
        ]

        for test_file in test_files:
            try:
                text = test_file.read_text(encoding="utf-8")
            except OSError:
                continue
            if any(pattern.search(text) for pattern in import_patterns):
                targets.add(test_file)

    return sorted(str(path.relative_to(ROOT)) for path in targets)


def git_changed_files(base: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", base],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run pytest only for Python files affected by the current change set.")
    parser.add_argument(
        "--base",
        default="HEAD",
        help="Git diff base used when --files is not provided (default: HEAD).",
    )
    parser.add_argument(
        "--files",
        nargs="*",
        help="Explicit changed files to resolve instead of querying git diff.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print resolved pytest targets without executing pytest.",
    )
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="Extra arguments forwarded to pytest after target resolution.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    changed_files = args.files if args.files else git_changed_files(args.base)
    python_files = [path for path in changed_files if is_repo_python_file(path)]
    targets = resolve_pytest_targets(python_files)

    if not python_files:
        print("No affected Python files detected; skipping pytest.")
        return 0

    if not targets:
        print("No affected pytest targets resolved from changed Python files.")
        return 0

    print("Affected pytest targets:")
    for target in targets:
        print(f"  - {target}")

    if args.dry_run:
        return 0

    extra_args = list(args.pytest_args)
    if extra_args and extra_args[0] == "--":
        extra_args = extra_args[1:]

    cmd = [sys.executable, "-m", "pytest", *targets, *extra_args]
    return subprocess.run(cmd, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
