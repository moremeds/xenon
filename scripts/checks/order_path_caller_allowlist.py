"""Layer 2b guard — restrict who can place, cancel or modify broker orders.

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

Futu (X3a): the future adapter `xenon.execution.futu_orders` is a second
target with its own allowlist (only `services/order_entry.py` and the module
itself). Raw Futu SDK mutations (`place_order`, `modify_order`,
`cancel_all_order`, `unlock_trade`) are allowed only inside that adapter: the
guard flags ANY reference to those attributes in any other Python file when
the receiver is a Futu trade context — a name or attribute assigned from
`Open*TradeContext(...)` (including aliases like `ctx = client._trd_ctx`,
`with ... as`, subclasses, and context attributes such as `self._trd_ctx`
created in another file), a bound-method capture (`send = ctx.place_order`),
or `getattr(ctx, "<mutation>")`. IB's `client.place_order(...)` is not a
Futu context and stays allowed.
Limits: flow-insensitive name propagation (a local `client =
OpenSecTradeContext()` in one function taints a same-named parameter in
another); dynamic access other than a literal `getattr` and contexts passed
through function parameters or return values are not traced; an unparseable
Python file's mutation calls are not scanned (the import fallback still is).

Reference: docs/reference/order-path-incident-history.md (Layer 2 guards).
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

_GUARD_PATH = "scripts/checks/order_path_caller_allowlist.py"
_ORDER_ENTRY = "src/xenon/api/services/order_entry.py"
_FUTU_ADAPTER = "src/xenon/execution/futu_orders.py"

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
        _ORDER_ENTRY,
        # Static analysis — the guard itself names the symbols.
        _GUARD_PATH,
    }
)

# Files allowed to import the Futu adapter. Same trust rule as _ALLOWLIST.
_FUTU_ALLOWLIST = frozenset({_FUTU_ADAPTER, _ORDER_ENTRY, _GUARD_PATH})

# Roots scanned (directories, and single files at the repo root). We scan
# widely so anything new in the repo gets caught.
_SCAN_ROOTS = ("src", "scripts", "web", "deploy", "docker", ".github", "pyproject.toml")

_PACKAGE = "xenon.execution"
_MODULE_NAMES = frozenset({"ib_place_order", "ib_order_manage"})
_FUTU_MODULE_NAMES = frozenset({"futu_orders"})

# Futu SDK trade-context mutations; only the adapter may call them.
_FUTU_MUTATIONS = frozenset({"place_order", "modify_order", "cancel_all_order", "unlock_trade"})
_FUTU_CONTEXT_RE = re.compile(r"^Open\w*TradeContext\w*$")


def _import_patterns(names: frozenset[str]) -> tuple[re.Pattern[str], ...]:
    """Line-by-line import patterns (fallback for non-Python / unparseable files)."""
    mods = "(?:" + "|".join(sorted(names)) + ")"
    return (
        re.compile(rf"^\s*from\s+xenon\.execution\.{mods}\s+import\b"),
        re.compile(rf"^\s*from\s+xenon\.execution\s+import\b[^#\n]*\b{mods}\b"),
        re.compile(rf"^\s*import\s+xenon\.execution\.{mods}\b"),
    )


_IMPORT_PATTERNS = _import_patterns(_MODULE_NAMES)
_FUTU_IMPORT_PATTERNS = _import_patterns(_FUTU_MODULE_NAMES)


def _python_import_linenos(source: str | ast.AST, names: frozenset[str] = _MODULE_NAMES) -> list[int] | None:
    """Line numbers of imports of the modules in `names`, via `ast`.

    Returns None when the source does not parse; the caller then falls back to
    the old line-by-line patterns. Those patterns match single-line imports
    only, so a multiline import in a file with a syntax error is not caught.
    """
    if isinstance(source, ast.AST):
        tree = source
    else:
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            return None
    linenos: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hit = any(a.name.startswith(f"{_PACKAGE}.") and a.name.split(".")[2] in names for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0:
                parts = module.split(".")
                from_module = module.startswith(f"{_PACKAGE}.") and len(parts) > 2 and parts[2] in names
                from_package = module == _PACKAGE and any(a.name in names for a in node.names)
                hit = from_module or from_package
            else:
                # `from .ib_place_order import x`, `from ..execution.ib_place_order import x`,
                # `from . import ib_place_order`
                hit = any(part in names for part in module.split(".")) or any(a.name in names for a in node.names)
        else:
            continue
        if hit:
            linenos.append(node.lineno)
    return linenos


def _dotted(node: ast.AST) -> str | None:
    """`a`, `a.b.c` for Name/Attribute chains; None otherwise."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return None if base is None else f"{base}.{node.attr}"
    return None


class _FutuContexts:
    """Futu trade-context names in one module (flow-insensitive, per file)."""

    def __init__(self, tree: ast.AST) -> None:
        self.ctors: set[str] = set()
        self.paths: set[str] = set()
        self.attrs: set[str] = set()
        self._nodes = list(ast.walk(tree))
        for node in self._nodes:
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if _FUTU_CONTEXT_RE.match(alias.name):
                        self.ctors.add(alias.asname or alias.name)
        self.propagate(frozenset())

    def propagate(self, foreign_attrs: frozenset[str]) -> bool:
        """Fixpoint name propagation with a cross-file attribute set.

        ``foreign_attrs`` holds context attribute names collected repo-wide
        (e.g. ``_trd_ctx`` from ``FutuClient``), so ``ctx = client._trd_ctx``
        keeps Futu identity. Returns True when anything new was added; the
        caller re-runs it across all files until a global fixpoint.
        """
        changed = True
        grew = False
        while changed:
            changed = False
            for node in self._nodes:
                if isinstance(node, ast.ClassDef) and node.name not in self.ctors:
                    if any(self._is_ctor_ref(b) for b in node.bases):
                        self.ctors.add(node.name)
                        changed = True
                        grew = True
                for target, value in self._bindings(node):
                    path = _dotted(target)
                    if path and path not in self.paths and self.is_context(value, foreign_attrs):
                        self.paths.add(path)
                        if isinstance(target, ast.Attribute):
                            self.attrs.add(target.attr)
                        changed = True
                        grew = True
        return grew

    @staticmethod
    def _bindings(node: ast.AST):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                yield target, node.value
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            yield node.target, node.value
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    yield item.optional_vars, item.context_expr

    def _is_ctor_ref(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.ctors or bool(_FUTU_CONTEXT_RE.match(node.id))
        if isinstance(node, ast.Attribute):
            return bool(_FUTU_CONTEXT_RE.match(node.attr))
        return False

    def is_context(self, node: ast.AST, foreign_attrs: frozenset[str] = frozenset()) -> bool:
        if isinstance(node, ast.Call):
            return self._is_ctor_ref(node.func)
        path = _dotted(node)
        if path is None:
            return False
        return path in self.paths or (isinstance(node, ast.Attribute) and node.attr in (self.attrs | foreign_attrs))


def _futu_mutation_linenos(tree: ast.AST, contexts: _FutuContexts, foreign_attrs: frozenset[str]) -> list[int]:
    """Lines referencing a Futu SDK mutation attribute on a Futu trade
    context — calls, bound-method capture (``send = ctx.place_order``), any
    other read/write of the attribute, or a literal
    ``getattr(ctx, "<mutation>")``."""
    linenos: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _FUTU_MUTATIONS
            and contexts.is_context(node.value, foreign_attrs)
        ):
            linenos.add(node.lineno)
            continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in _FUTU_MUTATIONS
            and contexts.is_context(node.args[0], foreign_attrs)
        ):
            linenos.add(node.lineno)
    return sorted(linenos)


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


def _load(repo_root: Path) -> list[tuple[str, Path, list[str], ast.AST | None]]:
    """Non-test candidate files: (rel, path, lines, parsed tree or None)."""
    out: list[tuple[str, Path, list[str], ast.AST | None]] = []
    for scan_root_rel in _SCAN_ROOTS:
        scan_root = repo_root / scan_root_rel
        if not scan_root.exists():
            continue
        for file_path in _iter_candidate_files(scan_root):
            rel = file_path.relative_to(repo_root).as_posix()
            if _is_test_file(rel) or rel == _GUARD_PATH:
                continue
            try:
                text = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            tree = None
            if file_path.suffix == ".py":
                try:
                    tree = ast.parse(text)
                except (SyntaxError, ValueError):
                    tree = None
            out.append((rel, file_path, text.splitlines(), tree))
    return out


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
        print("Allowlisted files (may import futu_orders):")
        for path in sorted(_FUTU_ALLOWLIST):
            print(f"  {path}")
        print(f"Only {_FUTU_ADAPTER} may call Futu SDK {', '.join(sorted(_FUTU_MUTATIONS))}.")
        return 0

    repo_root: Path = args.repo_root.resolve()
    violations: list[tuple[str, int, str]] = []
    files = _load(repo_root)

    # Context attributes (e.g. `self._trd_ctx = OpenSecTradeContext(...)`)
    # declared anywhere; another file using `x._trd_ctx.place_order` is caught.
    # Propagate with the repo-wide attribute set to a global fixpoint so an
    # ordinary alias (`ctx = client._trd_ctx`) keeps Futu identity too.
    contexts = {rel: _FutuContexts(tree) for rel, _, _, tree in files if tree is not None}
    foreign_attrs = frozenset(a for c in contexts.values() for a in c.attrs)
    changed = True
    while changed:
        changed = False
        for ctxs in contexts.values():
            changed |= ctxs.propagate(foreign_attrs)
        foreign_attrs = frozenset(a for c in contexts.values() for a in c.attrs)

    for rel, file_path, lines, tree in files:
        ib_allowed = rel in _ALLOWLIST
        futu_allowed = rel in _FUTU_ALLOWLIST
        is_adapter = rel == _FUTU_ADAPTER
        ast_linenos: set[int] | None = None
        if tree is not None:
            ast_linenos = set()
            if not ib_allowed:
                ast_linenos.update(_python_import_linenos(tree, _MODULE_NAMES) or [])
            if not futu_allowed:
                ast_linenos.update(_python_import_linenos(tree, _FUTU_MODULE_NAMES) or [])
            if not is_adapter:
                ast_linenos.update(_futu_mutation_linenos(tree, contexts[rel], foreign_attrs))
        for lineno, line in enumerate(lines, start=1):
            # Skip comment lines — Python `#`, JS/TS `//`, or YAML/TOML `#`.
            stripped = line.lstrip()
            if stripped.startswith(("#", "//")):
                continue
            if ast_linenos is not None:
                # Python file that parsed: imports and mutations come from the AST only.
                hit = lineno in ast_linenos
            else:
                hit = (
                    (not ib_allowed and any(p.search(line) for p in _IMPORT_PATTERNS))
                    or (not futu_allowed and any(p.search(line) for p in _FUTU_IMPORT_PATTERNS))
                )
            if _CLI_PATTERN.search(line) or hit:
                violations.append((rel, lineno, line.strip()))

    if not violations:
        print("OK — no unauthorized callers of the order-write cores.")
        return 0

    print(
        "FAIL — unauthorized reference to an order-write core, the Futu adapter, a raw Futu SDK "
        "mutation, or a removed order CLI.",
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
    print(
        f"Futu SDK place_order / modify_order / cancel_all_order / unlock_trade belong in {_FUTU_ADAPTER} only.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    for rel, lineno, line in violations:
        print(f"  {rel}:{lineno}: {line}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "If this caller is genuinely required, edit the _ALLOWLIST / _FUTU_ALLOWLIST in",
        file=sys.stderr,
    )
    print(
        "scripts/checks/order_path_caller_allowlist.py and document why.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
