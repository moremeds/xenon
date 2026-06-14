import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "release" / "version_sync_check.py"


def run(tmp_path, version_content, package_content, web_package_content=None):
    (tmp_path / "VERSION").write_text(version_content)
    (tmp_path / "package.json").write_text(package_content)
    if web_package_content is not None:
        (tmp_path / "web").mkdir(exist_ok=True)
        (tmp_path / "web" / "package.json").write_text(web_package_content)
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


def test_passes_when_web_package_matches(tmp_path):
    r = run(tmp_path, "0.0.1\n", '{"version": "0.0.1"}', '{"version": "0.0.1"}')
    assert r.returncode == 0, r.stderr


def test_fails_when_web_package_differs(tmp_path):
    r = run(tmp_path, "0.0.1\n", '{"version": "0.0.1"}', '{"version": "0.6.1"}')
    assert r.returncode != 0
    assert "web/package.json" in r.stderr


def test_real_repo_versions_are_in_sync():
    """Guards against re-drift: the committed VERSION, package.json, and
    web/package.json must all agree."""
    root = Path(__file__).resolve().parents[2]
    version = (root / "VERSION").read_text().strip()
    for rel in ("package.json", "web/package.json"):
        pkg = json.loads((root / rel).read_text())
        assert pkg["version"] == version, f"{rel}={pkg['version']!r} != VERSION={version!r}"
