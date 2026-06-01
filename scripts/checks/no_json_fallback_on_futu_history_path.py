"""Fail if any code under the Futu-history surface reintroduces JSON-file
fallback. Patterned after no_json_fallback_on_order_path.py.

Order-path guard does NOT cover Futu. /futu/portfolio retains a legacy
data/futu_portfolio.json read by design (see
project_postgres_migration_read_side_gap.md).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

WATCHED_PATHS = [
    "src/xenon/api/routes/futu_history.py",
    "src/xenon/api/routes/equity_curve_cross_broker.py",
    "src/xenon/api/services/futu_history.py",
    "web/app/api/futu/equity-curve",
    "web/app/api/futu/trades",
    "web/app/api/futu/performance",
    "web/app/api/futu/reconciliation",
    "web/app/api/futu/history",
    "web/app/api/equity-curve",
]

BANNED = [
    (re.compile(r"readDataFile\("), "readDataFile() not allowed"),
    (re.compile(r"readFile\(.*futu.*json"), "readFile against data/futu*.json"),
    (re.compile(r"json\.load.*futu.*json"), "json.load against data/futu*.json"),
    (re.compile(r"open\(.*data/futu.*json"), "open() against data/futu*.json"),
]


def walk(p: Path):
    if p.is_file():
        yield p
        return
    if not p.exists():
        return
    for child in p.rglob("*"):
        if child.is_file() and child.suffix in {".py", ".ts", ".tsx"}:
            yield child


def main() -> int:
    failures: list[str] = []
    for rel in WATCHED_PATHS:
        for f in walk(ROOT / rel):
            text = f.read_text(encoding="utf-8", errors="ignore")
            for pattern, reason in BANNED:
                for m in pattern.finditer(text):
                    line = text[: m.start()].count("\n") + 1
                    failures.append(f"{f.relative_to(ROOT)}:{line} — {reason}")
    if failures:
        print("FAIL — JSON-file fallback found on Futu-history path:")
        for f in failures:
            print(f"  {f}")
        return 1
    print(f"OK — no JSON-file fallback in {len(WATCHED_PATHS)} watched paths.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
