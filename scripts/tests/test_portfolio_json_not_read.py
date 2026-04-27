"""Regression: ensure no source file under src/ or scripts/ reads
data/portfolio.json at runtime.

Phase 2 of the postgres-read-path migration removed every reader of
data/portfolio.json. If a future PR re-introduces an open()/read_text()/
verified_load()/json.load() against that file, this test catches it
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
_READ_PATTERNS = re.compile(r"\b(open|read_text|verified_load|json\.load|json\.loads)\s*\([^)]*portfolio\.json")


def test_no_source_reads_portfolio_json():
    out = subprocess.check_output(
        [
            "git",
            "grep",
            "-n",
            "portfolio.json",
            "--",
            "src/",
            "scripts/",
            ":^scripts/migrations/",
            ":^scripts/tests/",
            ":^src/xenon/db/migrations/",
        ],
        cwd=REPO_ROOT,
    )
    offenders: list[str] = []
    for line in out.decode().splitlines():
        # Anything matching futu_portfolio.json is the separate Futu→PG
        # follow-up; ignore those entirely until that migration ships.
        if "futu_portfolio.json" in line:
            continue
        if _READ_PATTERNS.search(line):
            offenders.append(line)
    assert not offenders, "Source code still reads data/portfolio.json:\n" + "\n".join(offenders)
