"""Spec §13.8 T4 audit script."""
from __future__ import annotations

import json
import os
import subprocess


def test_audit_runs_clean_with_empty_data():
    env = {
        **os.environ,
        "DATABASE_URL": os.environ.get(
            "DATABASE_URL_TEST",
            "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
        ),
    }
    result = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/checks/no_duplicate_close_audit.py",
            "--since",
            "1d",
            "--scope-account",
            "DU0000000",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    body = json.loads(result.stdout or "{}")
    assert body.get("violations") == []
