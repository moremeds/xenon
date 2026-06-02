"""dev.sh refuses to start when DATABASE_URL points at core_dev.

Locks in the 'no MacBook writes to prod' rule from
docs/runbooks/dev-prod-db-cutover.md. Pure subprocess test — no PG, no
FastAPI, no IB Gateway required.

Test isolation: dev.sh reads its env file from XENON_ENV_FILE if set,
else $REPO_ROOT/.env. We point it at a per-test tmpdir stub so the
operator's live .env (which has DATABASE_URL_PAPER substituting
core_test for any core_dev DATABASE_URL in paper mode) cannot bypass
the guard during the test.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_SH = REPO_ROOT / "scripts" / "infra" / "dev.sh"


def _run_dev_sh(
    database_url: str,
    *,
    mode: str,
    env_file: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run dev.sh against a stub env file and return its CompletedProcess.

    The DB-name guard fires before the alembic upgrade and the IB
    Gateway probe, so the subprocess exits in milliseconds. We do not
    rely on the script reaching the broker-account stage.
    """
    env_file.write_text(f"DATABASE_URL={database_url}\n")
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "XENON_ENV_FILE": str(env_file),
        "XENON_PAPER_ACCOUNT": "DU9999999",
        "XENON_LIVE_ACCOUNT": "U9999999",
    }
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(DEV_SH), mode],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_dev_sh_refuses_core_dev(tmp_path: Path) -> None:
    """DATABASE_URL pointing at core_dev exits 2 with the FATAL marker."""
    result = _run_dev_sh(
        "postgresql+asyncpg://xenon_dev:pw@127.0.0.1:5432/core_dev",
        mode="paper",
        env_file=tmp_path / ".env",
    )
    assert result.returncode == 2, (
        f"Expected exit 2, got {result.returncode}.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "FATAL" in combined, f"Missing FATAL marker. stderr:\n{result.stderr}"
    assert "core_dev" in combined
    assert "Docker stack" in combined or "macmini" in combined


def test_dev_sh_refuses_core_dev_with_query_string(tmp_path: Path) -> None:
    """The db-name extraction strips ?queryparams correctly."""
    result = _run_dev_sh(
        "postgresql+asyncpg://xenon_dev:pw@127.0.0.1:5432/core_dev?sslmode=require",
        mode="paper",
        env_file=tmp_path / ".env",
    )
    assert result.returncode == 2, result.stderr
    combined = result.stdout + result.stderr
    assert "FATAL" in combined


def test_dev_sh_refuses_core_dev_in_live_mode(tmp_path: Path) -> None:
    """Live-mode DATABASE_URL=core_dev also hits the guard when no
    DATABASE_URL_TEST substitution exists. Covers the live-mode branch
    added in this PR (without the substitution available, core_dev
    must still trip the guard rather than silently proceed)."""
    result = _run_dev_sh(
        "postgresql+asyncpg://xenon_dev:pw@127.0.0.1:5432/core_dev",
        mode="live",
        env_file=tmp_path / ".env",
    )
    assert result.returncode == 2, (
        f"Expected exit 2, got {result.returncode}.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "FATAL" in (result.stdout + result.stderr)


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_dev_sh_passes_core_test(tmp_path: Path, mode: str) -> None:
    """Sanity: DATABASE_URL=core_test passes the guard in both modes.

    The script continues past the guard (alembic, IB probe, npm exec)
    so we don't care about the eventual exit — we only assert that the
    FATAL marker is NOT emitted. With the env-file injection we can
    now reliably test the PASS path in addition to the FAIL path.
    """
    result = _run_dev_sh(
        "postgresql+asyncpg://xenon_dev:pw@127.0.0.1:5432/core_test",
        mode=mode,
        env_file=tmp_path / ".env",
    )
    combined = result.stdout + result.stderr
    assert "FATAL: dev.sh refuses to start against core_dev" not in combined, (
        f"Guard fired when it shouldn't have. Output:\n{combined}"
    )
