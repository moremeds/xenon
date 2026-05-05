"""Smoke tests for the Docker IB Gateway helper."""

from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER = REPO_ROOT / "scripts" / "infra" / "docker_ib_gateway.sh"


def test_docker_ib_gateway_helper_help_resolves_compose_dir():
    result = subprocess.run(
        ["bash", str(HELPER), "help"],
        capture_output=True,
        text=True,
        timeout=5,
        cwd=str(REPO_ROOT),
    )

    assert result.returncode == 1
    assert "Usage:" in result.stdout
    assert "docker/ib-gateway" not in result.stderr
