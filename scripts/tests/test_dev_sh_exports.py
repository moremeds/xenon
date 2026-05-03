"""Verify scripts/infra/dev.sh exports XENON_BROKER_ACCOUNT per mode.

Prerequisite for the PG migration: every sync subprocess calls
AccountScope.resolve_from_env() at startup, which raises if
XENON_BROKER_ACCOUNT is unset. dev.sh must export a real per-mode account
from .env and fail loudly instead of inventing a fake fallback.

Approach: invoke dev.sh with the launcher short-circuited to print the env
vars and exit, instead of `exec npm run dev`. We do that by setting an env
var the test honors (XENON_DEV_SH_DRYRUN), wrapping the final exec.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_SH = REPO_ROOT / "scripts" / "infra" / "dev.sh"


@pytest.fixture
def dev_sh_dryrun(tmp_path):
    """Copy dev.sh into a tmp dir with the final `exec npm run dev` replaced
    by an env-dump so the test can inspect what was exported.
    """
    src = DEV_SH.read_text()
    # Strip the trailing block that cd's to web/ and execs npm run dev.
    # Replace with: dump the env vars we care about + exit 0.
    sentinel = 'log_info "Starting next dev + ib realtime + uvicorn (via npm run dev)…"'
    if sentinel not in src:
        pytest.fail("dev.sh layout changed — sentinel line not found; update this test")
    head, _ = src.split(sentinel, 1)
    dryrun = head + (
        'echo "XENON_TRADING_MODE=$XENON_TRADING_MODE"\n'
        'echo "XENON_BROKER_ACCOUNT=$XENON_BROKER_ACCOUNT"\n'
        'echo "IB_GATEWAY_HOST=$IB_GATEWAY_HOST"\n'
        'echo "IB_GATEWAY_PORT=$IB_GATEWAY_PORT"\n'
        "exit 0\n"
    )
    out = tmp_path / "dev.sh"
    out.write_text(dryrun)
    out.chmod(0o755)
    return out


def _run_dryrun(script: Path, mode: str, env_overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Run the trimmed dev.sh and parse its env-dump output."""
    env = os.environ.copy()
    # dev.sh sources alembic from .env; isolate the test by using a tmp .env.
    # Skip alembic by pointing DATABASE_URL at empty so the `if -n DATABASE_URL`
    # branch is skipped. Same for IB Gateway — the TCP probe is non-blocking.
    env["DATABASE_URL"] = ""
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        ["bash", str(script), mode],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        cwd=str(REPO_ROOT),
    )
    out = {}
    for line in result.stdout.splitlines():
        if "=" in line and not line.startswith("["):
            key, _, val = line.partition("=")
            out[key.strip()] = val.strip()
    return out


def test_paper_mode_requires_xenon_paper_account(dev_sh_dryrun):
    """Paper mode with no XENON_PAPER_ACCOUNT must exit non-zero."""
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    env = os.environ.copy()
    env["DATABASE_URL"] = ""
    env["XENON_PAPER_ACCOUNT"] = ""
    result = subprocess.run(
        ["bash", str(dev_sh_dryrun), "paper"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode != 0, (
        f"Paper mode without XENON_PAPER_ACCOUNT must fail; got returncode={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    combined = result.stderr + result.stdout
    assert "XENON_PAPER_ACCOUNT" in combined
    assert "clean-slate PG cutoff" in combined


def test_paper_mode_honors_xenon_paper_account(dev_sh_dryrun):
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    out = _run_dryrun(dev_sh_dryrun, "paper", env_overrides={"XENON_PAPER_ACCOUNT": "DU9876543"})
    assert out.get("XENON_BROKER_ACCOUNT") == "DU9876543"


def test_live_mode_requires_xenon_live_account(dev_sh_dryrun):
    """Live mode with no XENON_LIVE_ACCOUNT must exit non-zero."""
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    env = os.environ.copy()
    env["DATABASE_URL"] = ""
    env["XENON_LIVE_ACCOUNT"] = ""
    result = subprocess.run(
        ["bash", str(dev_sh_dryrun), "live"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode != 0, (
        f"Live mode without XENON_LIVE_ACCOUNT must fail; got returncode={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "XENON_LIVE_ACCOUNT" in (result.stderr + result.stdout)


def test_live_mode_honors_xenon_live_account(dev_sh_dryrun):
    if not shutil.which("bash"):
        pytest.skip("bash not on PATH")
    out = _run_dryrun(dev_sh_dryrun, "live", env_overrides={"XENON_LIVE_ACCOUNT": "U1234567"})
    assert out.get("XENON_BROKER_ACCOUNT") == "U1234567"
    assert out.get("XENON_TRADING_MODE") == "live"
