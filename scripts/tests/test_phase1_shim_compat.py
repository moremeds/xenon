"""Phase 1 shim --help compatibility tests.

One pytest case per Phase 1 Python shim. Invokes `<python> scripts/<old>.py --help`
via subprocess and asserts returncode 0. Catches import-path regressions and
shim-shape bugs (e.g., the runpy-at-import-time problem that broke 2d83ff4d's
apex_refresh shim before it was gated under __main__ in 8eb48119).

Deleted in Phase 2 PR 4 along with the shims this file covers.

Source of truth: this list mirrors scripts/infra/dev/smoke_phase1_shims.sh.
Adding or retiring a shim requires a one-line edit in BOTH files.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# 23 baseline Python shims (phase1-design.md §Baseline) minus the 2 dropped in
# commit 33b96e77 (ta_cli.py, ta_premarket_prep.py), plus apex_refresh added
# in PR 0 Task 1. Keep in sync with scripts/infra/dev/smoke_phase1_shims.sh.
SHIMS = [
    "fetch_flow.py",
    "fetch_ticker.py",
    "fetch_analyst_ratings.py",
    "fetch_menthorq_dashboard.py",
    "discover.py",
    "scanner.py",
    "kelly.py",
    "evaluate.py",
    "ib_order_manage.py",
    "ib_sync.py",
    "ib_option_chain.py",
    "leap_scanner_uw.py",
    "trend_scan.py",
    "uw_scan.py",
    "uw_analyze.py",
    "cri_scan.py",
    "vcg_scan.py",
    "gex_scan.py",
    "generate_gex_share.py",
    "generate_regime_share.py",
    "generate_cta_share.py",
    "generate_vcg_share.py",
    "test_ib_realtime.py",
    "apex_refresh.py",
]


@pytest.mark.parametrize("shim", SHIMS, ids=lambda s: s.removesuffix(".py"))
def test_phase1_shim_help_exits_zero(shim: str) -> None:
    """The shim at scripts/<shim> still resolves to a working --help invocation.

    Failure here means either the shim was deleted, the bucketed module was
    renamed/moved without updating the shim, or the shim shape regressed
    (e.g., import-time side effects breaking argparse).
    """
    shim_path = REPO_ROOT / "scripts" / shim
    assert shim_path.exists(), f"Shim missing at {shim_path}"

    result = subprocess.run(
        [sys.executable, str(shim_path), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=REPO_ROOT,
    )

    assert result.returncode == 0, (
        f"--help exited {result.returncode} for {shim}\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
