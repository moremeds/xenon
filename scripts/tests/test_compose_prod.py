"""Guard deploy/compose.prod.yml (prod deploy truth) and deploy/jobs/*.

Checks that need no YAML library and no Docker daemon:
- every service has an image, from the expected registry or a pinned tool image
- no literal secret values (secrets must be ${VAR} references)
- the dev build compose points at the prod file as deploy truth
- the job plists parse, run from the deployed compose file, and do not need a checkout

When the docker CLI exists, `docker compose config` must also accept the file.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROD_COMPOSE = REPO_ROOT / "deploy" / "compose.prod.yml"
DEV_COMPOSE = REPO_ROOT / "docker-compose.yml"
JOBS_DIR = REPO_ROOT / "deploy" / "jobs"

EXPECTED_SERVICES = {"migrator", "api", "web", "realtime", "watchtower"}
SECRET_NAME = re.compile(r"(TOKEN|PASS|SECRET|_PAT|_KEY|NOTIFICATION_URL|WEBHOOK)", re.IGNORECASE)
ENV_LINE = re.compile(r"^\s+([A-Z][A-Z0-9_]*):\s*(.*?)\s*$")
IMAGE_LINE = re.compile(r"^  ([a-z][a-z0-9_-]*):\s*$")


def _text() -> str:
    return PROD_COMPOSE.read_text()


def _service_images(text: str) -> dict[str, str | None]:
    """Map top-level service name -> image string (None when missing)."""
    out: dict[str, str | None] = {}
    current: str | None = None
    in_services = False
    for raw in text.splitlines():
        if raw.startswith("services:"):
            in_services = True
            continue
        if not in_services or not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw.startswith(" "):
            break
        m = IMAGE_LINE.match(raw)
        if m:
            current = m.group(1)
            out[current] = None
            continue
        if current and raw.startswith("    image:"):
            out[current] = raw.split("image:", 1)[1].strip()
    return out


def test_prod_compose_exists_and_has_deploy_truth_header() -> None:
    assert PROD_COMPOSE.is_file()
    head = "\n".join(_text().splitlines()[:30])
    assert "DEPLOY TRUTH" in head
    assert "/opt/xenon/compose.yml" in head
    assert "--profile migrate run --rm migrator" in head


def test_every_service_has_an_image() -> None:
    images = _service_images(_text())
    assert set(images) == EXPECTED_SERVICES
    for name, image in images.items():
        assert image, f"service {name} has no image"
        assert "build" not in image


def test_xenon_services_use_ghcr_and_watchtower_is_pinned() -> None:
    images = _service_images(_text())
    for name in ("migrator", "api", "web", "realtime"):
        assert images[name].startswith(f"ghcr.io/moremeds/xenon-{name}:"), images[name]
    assert re.fullmatch(r"containrrr/watchtower:\d+\.\d+\.\d+", images["watchtower"])


def test_no_build_sections_and_published_ports_bind_loopback() -> None:
    text = _text()
    assert not re.search(r"^\s+build:", text, re.MULTILINE)
    for m in re.finditer(r'^\s+- "([^"]*:\d+:\d+)"\s*$', text, re.MULTILINE):
        assert m.group(1).startswith("127.0.0.1:"), f"port not loopback-only: {m.group(1)}"


def test_no_literal_secret_values() -> None:
    bad: list[str] = []
    for raw in _text().splitlines():
        if raw.lstrip().startswith("#"):
            continue
        m = ENV_LINE.match(raw)
        if not m or not SECRET_NAME.search(m.group(1)):
            continue
        value = m.group(2).strip("\"'")
        if value and not value.startswith("${"):
            bad.append(f"{m.group(1)}={value[:12]}...")
    assert not bad, f"literal secret-like values in compose.prod.yml: {bad}"
    assert "<redacted" not in _text()


def test_watchtower_webhook_is_interpolated() -> None:
    assert "WATCHTOWER_NOTIFICATION_URL: ${WATCHTOWER_NOTIFICATION_URL}" in _text()


def test_only_opted_in_services_carry_watchtower_label() -> None:
    text = _text()
    assert text.count('com.centurylinklabs.watchtower.enable: "true"') == 3
    assert text.count('com.centurylinklabs.watchtower.enable: "false"') == 1  # watchtower itself
    assert 'profiles: ["migrate"]' in text


def test_dev_compose_header_points_to_prod_truth() -> None:
    head = "\n".join(DEV_COMPOSE.read_text().splitlines()[:6])
    assert "deploy/compose.prod.yml" in head


def _has_docker_compose() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _has_docker_compose(), reason="docker compose plugin not installed")
def test_docker_compose_config_accepts_prod_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("")
    (tmp_path / "web.env").write_text("")
    proc = subprocess.run(
        [
            "docker", "compose", "-f", str(PROD_COMPOSE), "--project-directory", str(tmp_path),
            "config", "--format", "json",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "GHCR_USER": "u", "GHCR_PAT": "p", "WATCHTOWER_NOTIFICATION_URL": "ntfy://dummy"},
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    cfg = json.loads(proc.stdout)
    assert set(cfg["services"]) == EXPECTED_SERVICES - {"migrator"}  # migrator is profile-gated
    for name, svc in cfg["services"].items():
        assert svc.get("image"), name


@pytest.mark.parametrize(
    ("plist", "hour", "minute"),
    [("com.xenon.nav-flex-refresh.plist", 17, 30), ("com.xenon.refresh-core-test.plist", 4, 0)],
)
def test_job_plists_parse_and_schedule(plist: str, hour: int, minute: int) -> None:
    data = plistlib.loads((JOBS_DIR / plist).read_bytes())
    assert data["Label"] == plist.removesuffix(".plist")
    assert data["StartCalendarInterval"] == {"Hour": hour, "Minute": minute}
    assert data["RunAtLoad"] is False
    assert data["EnvironmentVariables"]["TZ"] == "America/New_York"
    blob = json.dumps(data)
    assert "__XENON_ROOT__" not in blob and "projects/xenon" not in blob  # no checkout needed
    assert not re.search(r"(PASSWORD|TOKEN)\"\s*:\s*\"[^\"]", blob)


def test_nav_flex_job_runs_cli_from_deployed_image() -> None:
    args = plistlib.loads((JOBS_DIR / "com.xenon.nav-flex-refresh.plist").read_bytes())["ProgramArguments"]
    assert args[0].endswith("docker-compose")
    assert args[args.index("-f") + 1] == "/opt/xenon/compose.yml"
    assert "run" in args and "--rm" in args and "-T" in args
    assert "--name" in args  # fixed container name doubles as the lock
    assert "com.centurylinklabs.watchtower.enable=false" in args
    assert args[-2:] == ["api", "xenon-nav-flex-refresh"]


def test_job_clis_exist_in_pyproject_scripts() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert re.search(r"^xenon-nav-flex-refresh\s*=", pyproject, re.MULTILINE)
