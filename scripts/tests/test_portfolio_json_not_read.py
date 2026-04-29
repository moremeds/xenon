"""Regression: ensure no runtime source file reads
data/portfolio.json at runtime.

Phase 2 of the postgres-read-path migration removed every reader of
data/portfolio.json. If a future PR re-introduces an open()/read_text()/
verified_load()/json.load()/readFile() against that file, this test catches it
before it hits master.

Comments and docstrings that mention `portfolio.json` are allowed —
they document migration history. Only literal file-read calls trigger
the failure.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# A read is anything that would actually open the file at runtime.
_SEARCH_PATHS = (
    "src/",
    "scripts/",
    "web/app/api/",
)

_EXCLUDED_PREFIXES = (
    "scripts/migrations/",
    "scripts/tests/",
    "src/xenon/db/migrations/",
)

_READ_PATTERNS = re.compile(
    r"\b(open|read_text|verified_load|json\.load|json\.loads|readFile|readDataFile|readLocalJsonFile)"
    r"\s*\([^;]*(data/portfolio\.json|[\"']portfolio\.json[\"'])",
    re.DOTALL,
)


def test_no_source_reads_portfolio_json():
    out = subprocess.check_output(
        [
            "git",
            "ls-files",
            "portfolio.json",
            "--",
            *_SEARCH_PATHS,
            *[f":^{prefix}" for prefix in _EXCLUDED_PREFIXES],
        ],
        cwd=REPO_ROOT,
    )
    offenders: list[str] = []
    for rel_path in out.decode().splitlines():
        # Anything matching futu_portfolio.json is the separate Futu→PG
        # follow-up; ignore those entirely until that migration ships.
        if "futu_portfolio.json" in rel_path:
            continue
        content = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        lines = content.splitlines()
        for index, line in enumerate(lines):
            if "portfolio.json" not in line or "futu_portfolio.json" in line:
                continue
            start = max(0, index - 3)
            window = "\n".join(lines[start : index + 1])
            match = _READ_PATTERNS.search(window)
            if match:
                line_number = start + window[: match.start()].count("\n") + 1
                offenders.append(f"{rel_path}:{line_number}: {match.group(0).splitlines()[0]}")
    assert not offenders, "Source code still reads data/portfolio.json:\n" + "\n".join(offenders)
