"""CLI smoke: --help prints usage; --since with bad value errors out
before reaching the orchestrator.
"""

import subprocess


def test_help_exits_zero():
    r = subprocess.run(
        ["uv", "run", "xenon-futu-history-sync", "--help"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0
    assert "--since" in r.stdout
    assert "--account" in r.stdout


def test_invalid_since_returns_nonzero():
    r = subprocess.run(
        [
            "uv",
            "run",
            "xenon-futu-history-sync",
            "--account",
            "test",
            "--since",
            "yesterday",
        ],
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0
    assert "since" in r.stderr.lower()
