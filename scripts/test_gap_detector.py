#!/usr/bin/env python3
"""Detect source files without corresponding test files.

Scans Python (scripts/) and TypeScript (web/lib/, web/app/api/) source
directories and reports orphan files — source code with no matching test.

Usage:
    python scripts/test_gap_detector.py
    python scripts/test_gap_detector.py --json
    python scripts/test_gap_detector.py --max-orphans 30
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── Python scan ──────────────────────────────────────────────────────────


def _py_test_stems(test_dir: Path) -> set[str]:
    """Collect all test_ prefixed stems from the test directory."""
    stems: set[str] = set()
    for p in test_dir.rglob("test_*.py"):
        stems.add(p.stem)
    return stems


def _py_imported_modules(test_dir: Path) -> set[str]:
    """AST-parse test files to find imported module names."""
    imported: set[str] = set()
    for p in test_dir.rglob("test_*.py"):
        try:
            tree = ast.parse(p.read_text(errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[-1])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[-1])
    return imported


def scan_python() -> list[dict]:
    """Scan scripts/ for Python source files without tests."""
    scripts_dir = PROJECT_ROOT / "scripts"
    test_dir = scripts_dir / "tests"
    skip_names = {"__init__", "conftest", "__main__"}

    sources: list[Path] = []
    for p in scripts_dir.rglob("*.py"):
        if "tests" in p.parts or "__pycache__" in p.parts or "data" in p.parts or p.stem in skip_names:
            continue
        sources.append(p)

    test_stems = _py_test_stems(test_dir)
    imported = _py_imported_modules(test_dir)

    results = []
    for src in sorted(sources):
        stem = src.stem
        rel = src.relative_to(PROJECT_ROOT)
        # Match: foo.py → test_foo.py (or test_foo_*.py via prefix)
        has_direct = any(ts.startswith(f"test_{stem}") for ts in test_stems)
        has_import = stem in imported
        has_test = has_direct or has_import
        results.append(
            {
                "file": str(rel),
                "has_test": has_test,
                "test_file": next(
                    (ts for ts in sorted(test_stems) if ts.startswith(f"test_{stem}")),
                    None,
                ),
                "note": "import-only" if (has_import and not has_direct) else "",
            }
        )
    return results


# ── TypeScript scan ──────────────────────────────────────────────────────


def _ts_test_stems(test_dir: Path) -> set[str]:
    """Collect test file stems (without .test.ts suffix)."""
    stems: set[str] = set()
    for p in test_dir.rglob("*.test.ts"):
        stems.add(p.stem.removesuffix(".test"))
    for p in test_dir.rglob("*.test.tsx"):
        stems.add(p.stem.removesuffix(".test"))
    return stems


def _is_barrel_or_types(path: Path) -> bool:
    """Skip type-only and barrel re-export files."""
    if path.suffix == ".d.ts":
        return True
    if path.name in ("index.ts", "types.ts"):
        return True
    return False


def scan_typescript() -> list[dict]:
    """Scan web/lib/ and web/app/api/ for TS source files without tests."""
    web_dir = PROJECT_ROOT / "web"
    test_dir = web_dir / "tests"
    scan_dirs = [web_dir / "lib", web_dir / "app" / "api"]

    sources: list[Path] = []
    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        for p in scan_dir.rglob("*.ts"):
            if _is_barrel_or_types(p) or "node_modules" in p.parts:
                continue
            sources.append(p)
        for p in scan_dir.rglob("*.tsx"):
            if _is_barrel_or_types(p) or "node_modules" in p.parts:
                continue
            sources.append(p)

    test_stems = _ts_test_stems(test_dir)

    # Also check if file stem appears anywhere in test file contents
    all_test_content = ""
    for p in test_dir.rglob("*"):
        if p.is_file() and p.suffix in (".ts", ".tsx"):
            try:
                all_test_content += p.read_text(errors="ignore") + "\n"
            except OSError:
                continue

    results = []
    for src in sorted(sources):
        stem = src.stem
        rel = src.relative_to(PROJECT_ROOT)
        has_direct = stem in test_stems
        has_import = stem in all_test_content
        has_test = has_direct or has_import
        results.append(
            {
                "file": str(rel),
                "has_test": has_test,
                "test_file": f"{stem}.test.ts" if has_direct else None,
                "note": "import-only" if (has_import and not has_direct) else "",
            }
        )
    return results


# ── Output ───────────────────────────────────────────────────────────────


def print_markdown(results: list[dict], label: str) -> int:
    """Print markdown table and return orphan count."""
    orphans = [r for r in results if not r["has_test"]]
    covered = len(results) - len(orphans)
    pct = (covered / len(results) * 100) if results else 0

    print(f"\n## {label}")
    print(f"\n{covered}/{len(results)} files covered ({pct:.0f}%)\n")

    if orphans:
        print("| Source File | Has Test? | Test File | Notes |")
        print("|---|---|---|---|")
        for r in orphans:
            check = "✓" if r["has_test"] else "✗"
            tf = r["test_file"] or "—"
            print(f"| `{r['file']}` | {check} | {tf} | {r['note']} |")

    return len(orphans)


def main():
    parser = argparse.ArgumentParser(description="Detect source files without tests")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--max-orphans", type=int, default=50, help="Exit 1 if orphan count exceeds this (default 50)")
    args = parser.parse_args()

    py_results = scan_python()
    ts_results = scan_typescript()

    if args.json:
        print(json.dumps({"python": py_results, "typescript": ts_results}, indent=2))
    else:
        py_orphans = print_markdown(py_results, "Python (scripts/)")
        ts_orphans = print_markdown(ts_results, "TypeScript (web/lib/ + web/app/api/)")
        total_orphans = py_orphans + ts_orphans
        print(f"\n**Total orphans: {total_orphans}** (threshold: {args.max_orphans})")
        if total_orphans > args.max_orphans:
            print(f"⚠️  Exceeds threshold of {args.max_orphans}")
            sys.exit(1)


if __name__ == "__main__":
    main()
