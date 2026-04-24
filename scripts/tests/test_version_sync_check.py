import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "release" / "version_sync_check.py"


def run(tmp_path, version_content, package_content):
    (tmp_path / "VERSION").write_text(version_content)
    (tmp_path / "package.json").write_text(package_content)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
    )


def test_passes_when_versions_match(tmp_path):
    r = run(tmp_path, "0.0.1\n", '{"version": "0.0.1"}')
    assert r.returncode == 0, r.stderr


def test_fails_when_versions_differ(tmp_path):
    r = run(tmp_path, "0.0.1\n", '{"version": "0.6.1"}')
    assert r.returncode != 0
    assert "mismatch" in r.stderr.lower()
