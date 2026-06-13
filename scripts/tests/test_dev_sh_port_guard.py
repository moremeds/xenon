"""dev.sh refuses to start when the FastAPI port already has a listener.

Zombie uvicorn pairs (e.g. surviving a deleted worktree) otherwise coexist
with the fresh stack and serve stale code/env. Discovered 2026-06-13: two
FastAPI processes from the deleted performance-holistic-upgrade worktree
held 8321 and served a 9-day-old branch.

Pure subprocess test — no PG, no FastAPI, no IB Gateway required. The
port guard fires after the core_dev DB guard and before alembic, so the
subprocess exits in milliseconds.
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_SH = REPO_ROOT / "scripts" / "infra" / "dev.sh"


def _run_dev_sh(
    *,
    env_file: Path,
    extra_env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    env_file.write_text("DATABASE_URL=postgresql://xenon_dev@localhost:5432/core_test\n")
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "XENON_ENV_FILE": str(env_file),
        "XENON_PAPER_ACCOUNT": "DU9999999",
        "XENON_LIVE_ACCOUNT": "U9999999",
    }
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(DEV_SH), "paper"],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_dev_sh_refuses_busy_api_port(tmp_path: Path) -> None:
    """A bound API port exits 3 with the FATAL listener marker."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        proc = _run_dev_sh(
            env_file=tmp_path / ".env",
            extra_env={"XENON_API_PORT": str(port)},
        )
    finally:
        sock.close()
    assert proc.returncode == 3, proc.stderr
    assert "already has a listener" in proc.stderr
