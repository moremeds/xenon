"""dev.sh refuses to start when DATABASE_URL points at core_dev.

Locks in the 'no MacBook writes to prod' rule from
docs/runbooks/dev-prod-db-cutover.md. Pure subprocess test — no PG, no
FastAPI, no IB Gateway required.

Coverage note: we intentionally only test the FAIL case. Asserting the
PASS case (`core_test` URL advances past the guard) is fragile because
the script unconditionally sources `$REPO_ROOT/.env`, and a local .env
holding the legacy `core_dev` URL would override our injection. The
guard's correctness is regression-proven by the FAIL cases below.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_SH = REPO_ROOT / "scripts" / "infra" / "dev.sh"


def _run_dev_sh(
    database_url: str,
    mode: str = "paper",
) -> subprocess.CompletedProcess[str]:
    """Run dev.sh against a stub env and return its CompletedProcess.

    The DB-name guard fires before the alembic upgrade and the IB
    Gateway probe, so the subprocess exits in milliseconds. We do not
    rely on the script reaching the broker-account stage.
    """
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "DATABASE_URL": database_url,
        "XENON_PAPER_ACCOUNT": "DU9999999",
        "XENON_LIVE_ACCOUNT": "U9999999",
    }
    return subprocess.run(
        ["bash", str(DEV_SH), mode],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_dev_sh_refuses_core_dev() -> None:
    """DATABASE_URL pointing at core_dev exits 2 with the FATAL marker."""
    result = _run_dev_sh(
        "postgresql+asyncpg://xenon_dev:pw@127.0.0.1:5432/core_dev",
        mode="paper",
    )
    assert result.returncode == 2, (
        f"Expected exit 2, got {result.returncode}.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "FATAL" in combined, f"Missing FATAL marker. stderr:\n{result.stderr}"
    assert "core_dev" in combined
    assert "Docker stack" in combined or "macmini" in combined


def test_dev_sh_refuses_core_dev_with_query_string() -> None:
    """The db-name extraction strips ?queryparams correctly."""
    result = _run_dev_sh(
        "postgresql+asyncpg://xenon_dev:pw@127.0.0.1:5432/core_dev?sslmode=require",
        mode="paper",
    )
    assert result.returncode == 2, result.stderr
    combined = result.stdout + result.stderr
    assert "FATAL" in combined
