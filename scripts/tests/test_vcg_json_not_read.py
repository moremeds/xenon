"""Regression: ensure no source file under web/, src/, or scripts/ reads
data/vcg.json at runtime.

Mirrors test_portfolio_json_not_read.py for the VCG migration —
xenon.vcg_series.payload (Postgres) is the source of truth; the JSON
file is at most a write-side artifact that nothing should read on the
hot path.

Comments and docstrings that mention `vcg.json` are allowed — they
document migration history. Only literal file-read calls trigger the
failure.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# A read is anything that would actually open the file at runtime.
_PY_READ_PATTERNS = re.compile(
    r"\b(open|read_text|verified_load|json\.load|json\.loads|_read_cache)\s*\([^)]*vcg\.json"
)
_TS_READ_PATTERNS = re.compile(r"\b(readFile|readFileSync|readDataFile)\s*\([^)]*vcg\.json")


def test_no_source_reads_vcg_json():
    out = subprocess.check_output(
        [
            "git",
            "grep",
            "-n",
            "vcg.json",
            "--",
            "src/",
            "scripts/",
            "web/",
            ":^scripts/migrations/",
            ":^scripts/tests/",
            ":^src/xenon/db/migrations/",
            ":^web/node_modules/",
            ":^web/.next/",
        ],
        cwd=REPO_ROOT,
    )
    offenders: list[str] = []
    for line in out.decode().splitlines():
        if _PY_READ_PATTERNS.search(line) or _TS_READ_PATTERNS.search(line):
            offenders.append(line)
    assert not offenders, "Source code still reads data/vcg.json at runtime:\n" + "\n".join(offenders)
