"""Layer 2b guard — restrict who can place, cancel or modify IB orders.

Background
----------
Order writes run through the connected-client cores in
`xenon.execution.ib_place_order` (place) and `xenon.execution.ib_order_manage`
(cancel / modify). Since PR-10 the only caller is the order-entry service
(`src/xenon/api/services/order_entry.py`, moved out of `server.py` in PR-20),
which runs them on the API's single order owner (`api/order_owner.py`, pool
`orders` connection) *only after* `_run_preflight()`, Gate 4 and
`_validate_non_combo_quote()` have approved the request. There is no
standalone CLI any more. Any other caller — direct Python import or a
revived console script — sidesteps Gate 4, the preflight, the quote gate,
idempotency and the single owner.

This is exactly the [In-process route bypass] regression class that has
already burned the repo twice (#34 quote_token, #47 audit gating).

This guard fails the build if any non-allowlisted file imports the
order-write modules, or if ANY file (server.py and pyproject.toml included)
names the removed console scripts `xenon-ib-place-order` /
`xenon-ib-order-manage`. Python files are parsed with `ast`, so multiline and
relative imports are caught; other files are matched line by line.

Reference: docs/reference/order-path-incident-history.md (Layer 2 guards).
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

# Files allowed to import the order-write modules. Adding to this list
# is a deliberate trust extension — review the implications against the
# preflight / quote-gate / idempotency / single-owner contracts first.
_ALLOWLIST = frozenset(
    {
        # The modules themselves.
        "src/xenon/execution/ib_place_order.py",
        "src/xenon/execution/ib_order_manage.py",
        # The single legitimate runtime caller — runs the cores on the
        # order owner after preflight + Gate 4 + quote gate.
        "src/xenon/api/services/order_entry.py",
        # Static analysis — the guard itself names the symbols.
        "scripts/checks/order_path_caller_allowlist.py",
    }
)

# Roots scanned (directories, and single files at the repo root). We scan
# widely so anything new in the repo gets caught.
_SCAN_ROOTS = ("src", "scripts", "web", "deploy", "docker", ".github", "pyproject.toml")

_MODULES = r"(?:ib_place_order|ib_order_manage)"

# Imports of the order-write modules (allowlisted files may import them).
_IMPORT_PATTERNS = (
    re.compile(rf"^\s*from\s+xenon\.execution\.{_MODULES}\s+import\b"),
    re.compile(rf"^\s*from\s+xenon\.execution\s+import\b[^#\n]*\b{_MODULES}\b"),
    re.compile(rf"^\s*import\s+xenon\.execution\.{_MODULES}\b"),
)

_MODULE_NAMES = frozenset({"ib_place_order", "ib_order_manage"})
_PACKAGE = "xenon.execution"


def _python_import_linenos(source: str) -> list[int] | None:
    """Line numbers of imports of the order-write modules, via `ast`.

    Returns None when the source does not parse; the caller then falls back to
    the line-by-line patterns so a syntax error can never hide an import.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    linenos: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hit = any(a.name.startswith(f"{_PACKAGE}.") and a.name.split(".")[2] in _MODULE_NAMES for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0:
                parts = module.split(".")
                from_module = module.startswith(f"{_PACKAGE}.") and len(parts) > 2 and parts[2] in _MODULE_NAMES
                from_package = module == _PACKAGE and any(a.name in _MODULE_NAMES for a in node.names)
                hit = from_module or from_package
            else:
                # `from .ib_place_order import x` / `from . import ib_place_order`
                hit = module.split(".")[0] in _MODULE_NAMES or any(a.name in _MODULE_NAMES for a in node.names)
        else:
            continue
        if hit:
            linenos.append(node.lineno)
    return linenos


# The removed console scripts: forbidden everywhere except this guard.
_CLI_PATTERN = re.compile(r"\bxenon-ib-(?:place-order|order-manage)\b")

_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".mjs", ".sh", ".toml", ".yml", ".yaml", ".plist", ".json")

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
    if root.is_file():
        return [root]
    files: list[Path] = []
    for ext in _EXTENSIONS:
        files.extend(root.rglob(f"*{ext}"))
    files.extend(root.rglob("*Dockerfile*"))
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
        print("Allowlisted files (may import ib_place_order / ib_order_manage):")
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
            if _is_test_file(rel) or rel == "scripts/checks/order_path_caller_allowlist.py":
                continue
            allowlisted = rel in _ALLOWLIST
            try:
                lines = file_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            ast_linenos: set[int] | None = None
            if not allowlisted and file_path.suffix == ".py":
                found = _python_import_linenos("\n".join(lines))
                ast_linenos = None if found is None else set(found)
            for lineno, line in enumerate(lines, start=1):
                # Skip comment lines — Python `#`, JS/TS `//`, or YAML/TOML `#`.
                stripped = line.lstrip()
                if stripped.startswith(("#", "//")):
                    continue
                if ast_linenos is not None:
                    # Python file that parsed: imports come from the AST only.
                    import_hit = lineno in ast_linenos
                else:
                    import_hit = not allowlisted and any(p.search(line) for p in _IMPORT_PATTERNS)
                if _CLI_PATTERN.search(line) or import_hit:
                    violations.append((rel, lineno, line.strip()))

    if not violations:
        print("OK — no unauthorized callers of the order-write cores.")
        return 0

    print(
        "FAIL — unauthorized reference to an order-write core or a removed order CLI.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print(
        "Order writes must go through src/xenon/api/services/order_entry.py (order owner) after",
        file=sys.stderr,
    )
    print(
        "_run_preflight() + _validate_non_combo_quote() have approved the",
        file=sys.stderr,
    )
    print(
        "request. Direct imports or CLI invocations bypass Gate 4 and the owner and have",
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
