"""CI guard: position rule modules must not read live bracket policy config.

Why: rule config is frozen into `position_protection.config` when a protection
row is inserted. If a rule module reads `bracket_policies` directly, changing
policy rows could retune an already-armed position mid-flight.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

RULES_DIR = Path("src/xenon/execution/brackets/rules")
FORBIDDEN_TOKEN = "bracket_policies"


def check_module(path: Path) -> list[str]:
    source = path.read_text()
    violations: list[str] = []

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"{path}: syntax error: {exc}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if FORBIDDEN_TOKEN in module:
                violations.append(f"{path}:{node.lineno}: import from `{module}` references {FORBIDDEN_TOKEN}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if FORBIDDEN_TOKEN in alias.name:
                    violations.append(f"{path}:{node.lineno}: import `{alias.name}` references {FORBIDDEN_TOKEN}")

    for line_no, line in enumerate(source.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if FORBIDDEN_TOKEN in line and "import" not in line:
            violations.append(f"{path}:{line_no}: reference to {FORBIDDEN_TOKEN}")

    return violations


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    target = Path(argv[0]) if argv else RULES_DIR
    files = list(target.rglob("*.py")) if target.is_dir() else [target]
    checked = [file for file in files if file.name != "__init__.py"]

    violations: list[str] = []
    for file in checked:
        violations.extend(check_module(file))

    if violations:
        print("frozen_config_at_arm: violations:", file=sys.stderr)
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        return 1

    print(f"frozen_config_at_arm: {len(checked)} file(s) clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
