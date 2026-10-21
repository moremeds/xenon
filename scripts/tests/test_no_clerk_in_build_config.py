"""Clerk was removed in #186; release/build config must not reference it."""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _build_config_files() -> list[Path]:
    files = list((REPO / ".github" / "workflows").glob("*.y*ml"))
    files += list((REPO / "docker").glob("*Dockerfile*"))
    files += list((REPO / "deploy").rglob("*"))
    return [p for p in files if p.is_file()]


def test_build_and_deploy_config_has_no_clerk_reference():
    offenders = [
        str(p.relative_to(REPO))
        for p in _build_config_files()
        if "clerk" in p.read_text(errors="ignore").lower()
    ]
    assert offenders == []
