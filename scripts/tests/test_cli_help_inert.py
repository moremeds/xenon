"""Every ``[project.scripts]`` entry point must answer ``--help`` with exit 0 and no side effects.

Each CLI runs in a subprocess with a broker-deny environment: Python sockets
refuse to connect, the database URLs point at a closed port, Flex credentials
are unset and READ_ONLY is off (so a CLI that ignores ``--help`` and starts its
job fails loudly instead of being skipped by the READ_ONLY guard).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTRY_POINTS = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]

_RUNNER = """
import importlib, socket, sys

def _deny(*args, **kwargs):
    raise OSError("network denied: --help must not touch a broker or database")

socket.socket.connect = _deny
socket.socket.connect_ex = _deny
socket.create_connection = _deny

module_name, func_name = sys.argv[1].split(":")
sys.argv = [module_name, "--help"]
result = getattr(importlib.import_module(module_name), func_name)()
sys.exit(result if isinstance(result, int) else 0)
"""

_DEAD_DB = "postgresql+psycopg://u@127.0.0.1:1/xe_help_check"


@pytest.mark.parametrize("name", sorted(ENTRY_POINTS))
def test_entry_point_help_is_inert(name: str) -> None:
    env = {
        **os.environ,
        "DATABASE_URL": _DEAD_DB,
        "DATABASE_URL_TEST": _DEAD_DB,
        "XENON_READ_ONLY": "",
        "IB_FLEX_TOKEN": "",
        "IB_FLEX_NAV_QUERY_ID": "",
        "IB_FLEX_QUERY_ID": "",
        "UW_TOKEN": "",
    }
    proc = subprocess.run(
        [sys.executable, "-c", _RUNNER, ENTRY_POINTS[name]],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        cwd=ROOT,
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"{name} --help exited {proc.returncode}:\n{output[-800:]}"
    assert "usage" in output.lower(), f"{name} --help printed no usage text:\n{output[-800:]}"
