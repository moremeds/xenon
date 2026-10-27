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
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROD_COMPOSE = REPO_ROOT / "deploy" / "compose.prod.yml"
DEV_COMPOSE = REPO_ROOT / "docker-compose.yml"
JOBS_DIR = REPO_ROOT / "deploy" / "jobs"

EXPECTED_SERVICES = {"migrator", "api", "web", "realtime", "futu-quotes", "watchtower"}
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
    assert images["futu-quotes"] == images["api"]  # same image, different command
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
    assert text.count('com.centurylinklabs.watchtower.enable: "true"') == 4
    assert text.count('com.centurylinklabs.watchtower.enable: "false"') == 1  # watchtower itself
    assert 'profiles: ["migrate"]' in text


def test_cross_container_urls_and_web_healthcheck() -> None:
    """v0.9.0 shipped without these: the relay defaults WS_HOST and its ticket
    validator to 127.0.0.1 (its own loopback, not the published port or the api),
    the api probed relay /status on its own loopback, and web probed the deleted
    /sign-in page."""
    services = yaml.safe_load(_text())["services"]
    rt_env = services["realtime"]["environment"]
    assert rt_env["WS_HOST"] == "0.0.0.0"
    assert rt_env["TICKET_VALIDATE_URL"] == "http://api:8321/ws-ticket/validate"
    assert services["api"]["environment"]["IB_REALTIME_STATUS_URL"] == "http://realtime:8765/status"
    assert services["web"]["healthcheck"]["test"][-1] == "http://127.0.0.1:3000/"
    assert rt_env["FUTU_QUOTES_URL"] == "ws://futu-quotes:8767"


def test_futu_quotes_has_no_secrets_and_no_host_port() -> None:
    fq = yaml.safe_load(_text())["services"]["futu-quotes"]
    assert "env_file" not in fq and "volumes" not in fq and "ports" not in fq
    assert fq["environment"]["FUTU_OPEND_HOST"] == "host.docker.internal"


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


ET = ZoneInfo("America/New_York")
HKT = ZoneInfo("Asia/Hong_Kong")  # system zone of the Mac mini; launchd and cron fire in it
# job plist -> (ET hour, ET minute) the job is meant to run at
JOBS = {
    "com.xenon.nav-flex-refresh.plist": (17, 30),
    "com.xenon.refresh-core-test.plist": (4, 0),
    "com.xenon.flex-divergence.plist": (7, 30),
}
# One day in each DST state of New York.
DST_DAYS = {"EDT": date(2026, 7, 15), "EST": date(2026, 1, 15)}
GUARD = re.compile(r'\[ "\$\(TZ=America/New_York date \+\\?%H\)" = (\d\d) \]')


def _plist(name: str) -> dict:
    return plistlib.loads((JOBS_DIR / name).read_bytes())


def _hkt_times(plist: dict) -> list[tuple[int, int]]:
    cal = plist["StartCalendarInterval"]
    cal = cal if isinstance(cal, list) else [cal]
    return [(c["Hour"], c["Minute"]) for c in cal]


def _fires_et_hour(hkt: tuple[int, int], day: date) -> tuple[int, int]:
    """ET (hour, minute) of a daily HKT firing that lands on the given ET day's DST state."""
    noon_utc = datetime(day.year, day.month, day.day, 12, tzinfo=ZoneInfo("UTC"))
    hkt_day = noon_utc.astimezone(HKT).date()
    fired = datetime(hkt_day.year, hkt_day.month, hkt_day.day, hkt[0], hkt[1], tzinfo=HKT)
    et = fired.astimezone(ET)
    return et.hour, et.minute


def _guard_hour(command: str) -> int:
    m = GUARD.search(command)
    assert m, f"no ET-hour guard in: {command}"
    return int(m.group(1))


@pytest.mark.parametrize("plist_name", sorted(JOBS))
def test_job_plists_parse_and_have_et_hour_guard(plist_name: str) -> None:
    data = _plist(plist_name)
    et_hour, _ = JOBS[plist_name]
    assert data["Label"] == plist_name.removesuffix(".plist")
    assert data["RunAtLoad"] is False
    args = data["ProgramArguments"]
    assert args[:2] == ["/bin/sh", "-c"] and len(args) == 3
    assert _guard_hour(args[2]) == et_hour
    assert "|| exit 0;" in args[2]  # guard skips with success, never fails the job
    blob = json.dumps(data)
    assert "__XENON_ROOT__" not in blob and "projects/xenon" not in blob  # no checkout needed
    assert not re.search(r"(PASSWORD|TOKEN)\"\s*:\s*\"[^\"]", blob)


@pytest.mark.parametrize("plist_name", sorted(JOBS))
def test_job_hkt_times_hit_the_et_time_in_both_dst_states(plist_name: str) -> None:
    et_hour, et_minute = JOBS[plist_name]
    times = _hkt_times(_plist(plist_name))
    assert len(times) == 2, "need one candidate HKT time per DST state"
    for state, day in DST_DAYS.items():
        landing = [_fires_et_hour(t, day) for t in times]
        # exactly one firing lands on the stated ET time; the guard rejects the other
        assert landing.count((et_hour, et_minute)) == 1, (state, times, landing)
        other = next(x for x in landing if x != (et_hour, et_minute))
        assert other[0] != et_hour, "second firing must fail the ET-hour guard"


def test_cron_entries_have_et_hour_guard_and_candidate_hkt_times() -> None:
    lines = [
        ln for ln in (JOBS_DIR / "crontab.example").read_text().splitlines()
        if ln and not ln.startswith("#") and not ln.startswith("PATH=")
    ]
    assert len(lines) == len(JOBS)
    expected = {
        "xenon-nav-flex-refresh": (17, 30),
        "refresh-core-test.sh": (4, 0),
        "xenon-flex-divergence-check": (7, 30),
    }
    for line in lines:
        minute, hours, *_ = line.split()
        key = next(k for k in expected if k in line)
        et_hour, et_minute = expected[key]
        assert "\\%H" in line, "percent must be escaped in crontab"
        assert _guard_hour(line) == et_hour
        times = [(int(h), int(minute)) for h in hours.split(",")]
        assert len(times) == 2
        for day in DST_DAYS.values():
            landing = [_fires_et_hour(t, day) for t in times]
            assert landing.count((et_hour, et_minute)) == 1, (line, landing)


def test_nav_flex_job_runs_cli_from_deployed_image() -> None:
    cmd = _plist("com.xenon.nav-flex-refresh.plist")["ProgramArguments"][2]
    assert "exec /opt/homebrew/bin/docker-compose -f /opt/xenon/compose.yml run --rm -T" in cmd
    assert "--name xenon-job-nav-flex-refresh" in cmd  # fixed container name doubles as the lock
    assert "--label com.centurylinklabs.watchtower.enable=false" in cmd
    assert cmd.endswith(" api xenon-nav-flex-refresh")


def test_job_clis_exist_in_pyproject_scripts() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text()
    assert re.search(r"^xenon-nav-flex-refresh\s*=", pyproject, re.MULTILINE)
    assert re.search(r"^xenon-flex-divergence-check\s*=", pyproject, re.MULTILINE)


def test_flex_divergence_job_applies_from_deployed_image() -> None:
    """The Operator console Flex Divergence tile reads the row this job writes."""
    cmd = _plist("com.xenon.flex-divergence.plist")["ProgramArguments"][2]
    rm = "docker rm xenon-job-flex-divergence >/dev/null 2>&1;"
    assert cmd.index("exit 0;") < cmd.index(rm) < cmd.index("exec /opt/homebrew/bin/docker-compose")
    assert "--name xenon-job-flex-divergence" in cmd
    assert "--label com.centurylinklabs.watchtower.enable=false" in cmd
    assert cmd.endswith(" api xenon-flex-divergence-check --apply")


def test_nav_flex_job_clears_a_dead_job_container_without_force() -> None:
    """A container left by a failed start must not block later runs; a running one stays."""
    cmd = _plist("com.xenon.nav-flex-refresh.plist")["ProgramArguments"][2]
    rm = "docker rm xenon-job-nav-flex-refresh >/dev/null 2>&1;"
    assert rm in cmd
    # after the ET-hour guard, before docker-compose run
    assert cmd.index("exit 0;") < cmd.index(rm) < cmd.index("exec /opt/homebrew/bin/docker-compose")
    assert "docker rm -f" not in cmd and "--force" not in cmd and "docker kill" not in cmd
    cron = [ln for ln in (JOBS_DIR / "crontab.example").read_text().splitlines()
            if "xenon-nav-flex-refresh" in ln and ln.startswith("30 ")][0]
    assert "docker rm xenon-job-nav-flex-refresh >/dev/null 2>&1;" in cron
    assert "docker rm -f" not in cron


def test_plist_comments_give_manual_unguarded_run_and_kickstart_caveat() -> None:
    nav = (JOBS_DIR / "com.xenon.nav-flex-refresh.plist").read_text()
    assert "exits 0 BEFORE Docker starts" in nav
    assert "REAL WRITES" in nav and "PG persist failed" in nav
    assert "Manual unguarded run" in (JOBS_DIR / "com.xenon.refresh-core-test.plist").read_text()
    assert "lockf" not in (JOBS_DIR / "crontab.example").read_text().replace("no outer lockf", "")


# ---- scripts/infra/refresh-core-test.sh: destination guard and lock, with stub PG tools ----

REFRESH_SH = REPO_ROOT / "scripts" / "infra" / "refresh-core-test.sh"


def _stub_pg(tmp_path: Path, sleep_s: float = 0.0) -> tuple[Path, Path]:
    """Stub pg_dump/pg_restore/psql that only record their calls (no real PG is touched)."""
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    for name in ("pg_dump", "pg_restore", "psql"):
        stub = bindir / name
        stub.write_text(
            "#!/bin/sh\n"
            f'echo "{name} $*" >> "{calls}"\n'
            f"sleep {sleep_s}\n"
            '[ "$(basename "$0")" = pg_restore ] && cat > /dev/null\n'
            '[ "$(basename "$0")" = pg_dump ] && echo data\n'
            "exit 0\n"
        )
        stub.chmod(0o755)
    return bindir, calls


def _refresh_env(tmp_path: Path, bindir: Path, src: str, dst: str) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "XENON_REFRESH_PG_BIN": str(bindir),
        "XENON_REFRESH_LOG_DIR": str(tmp_path / "log"),
        "XENON_REFRESH_LOCK_DIR": str(tmp_path / "lock.d"),
        "XENON_REFRESH_SRC": src,
        "XENON_REFRESH_DST": dst,
        "PGPASSWORD": "stub",
    }


def _run_refresh(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(REFRESH_SH), *args], env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize(
    ("src", "dst"),
    [("core_test", "core_dev"), ("core_dev", "core_dev"), ("core_test", "core_test"),
     ("core_dev", "xenon_prod"), ("core_dev", "core_dev_copy")],
)
def test_refresh_refuses_bad_destination_before_any_pg_call(tmp_path: Path, src: str, dst: str) -> None:
    bindir, calls = _stub_pg(tmp_path)
    for extra in ((), ("--dry",)):
        proc = _run_refresh(_refresh_env(tmp_path, bindir, src, dst), *extra)
        assert proc.returncode == 2, proc.stdout + proc.stderr
    assert not calls.exists(), calls.read_text()


def test_refresh_allows_core_dev_to_core_test_and_releases_lock(tmp_path: Path) -> None:
    bindir, calls = _stub_pg(tmp_path)
    env = _refresh_env(tmp_path, bindir, "core_dev", "core_test")
    proc = _run_refresh(env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    recorded = calls.read_text()
    assert "psql" in recorded and "pg_dump" in recorded and "pg_restore" in recorded
    assert not (tmp_path / "lock.d").exists()  # lock released on exit
    assert _run_refresh(env).returncode == 0  # and a later run is not blocked


@pytest.mark.parametrize("extra", [(), ("--dry",)])
def test_refresh_dumps_only_app_schemas_as_xenon_dev(tmp_path: Path, extra: tuple[str, ...]) -> None:
    """The limited xenon_dev role cannot read other apps' schemas: pg_dump must be scoped to xenon + events."""
    bindir, calls = _stub_pg(tmp_path)
    proc = _run_refresh(_refresh_env(tmp_path, bindir, "core_dev", "core_test"), *extra)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    dumps = [line for line in calls.read_text().splitlines() if line.startswith("pg_dump ")]
    assert len(dumps) == 1, dumps
    assert " -n xenon -n events " in dumps[0] + " ", dumps[0]
    assert "-U xenon_dev" in dumps[0], dumps[0]


def test_refresh_second_concurrent_run_exits_without_calling_pg(tmp_path: Path) -> None:
    bindir, calls = _stub_pg(tmp_path, sleep_s=1.5)
    env = _refresh_env(tmp_path, bindir, "core_dev", "core_test")
    first = subprocess.Popen(["bash", str(REFRESH_SH)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 20
        while not calls.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert calls.exists(), "first run never reached the PG stub"
        before = calls.read_text()
        second = _run_refresh(env)
        assert second.returncode == 3, second.stdout + second.stderr
        assert calls.read_text() == before  # the loser called no PG command
    finally:
        out, err = first.communicate(timeout=60)
    assert first.returncode == 0, out + err
    assert not (tmp_path / "lock.d").exists()


def test_refresh_stale_lock_gives_exit_3_and_no_pg_call(tmp_path: Path) -> None:
    bindir, calls = _stub_pg(tmp_path)
    env = _refresh_env(tmp_path, bindir, "core_dev", "core_test")
    lock = tmp_path / "lock.d"
    lock.mkdir()
    dead = subprocess.Popen(["true"])
    dead.wait()
    (lock / "pid").write_text(str(dead.pid))
    proc = _run_refresh(env)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert not calls.exists()  # no PG command
    assert lock.is_dir() and (lock / "pid").read_text() == str(dead.pid)  # left for the operator
    log = (tmp_path / "log" / "refresh-core-test.log").read_text()
    assert "rm -rf" in log and "check that pid" in log  # recovery instructions


def test_refresh_exit_trap_keeps_a_lock_it_does_not_own(tmp_path: Path) -> None:
    """If another run replaced the lock owner mid-run, our EXIT trap must leave the lock alone."""
    bindir, calls = _stub_pg(tmp_path)
    lock = tmp_path / "lock.d"
    # first psql call (inside the locked section) simulates a successor taking over the lock
    (bindir / "psql").write_text(
        "#!/bin/sh\n"
        f'echo "psql $*" >> "{calls}"\n'
        f'echo 424242 > "{lock}/pid"\n'
        "exit 0\n"
    )
    env = _refresh_env(tmp_path, bindir, "core_dev", "core_test")
    proc = _run_refresh(env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert lock.is_dir() and (lock / "pid").read_text().strip() == "424242"


def test_api_stop_budget_covers_the_graceful_ib_disconnect() -> None:
    """PR-10: compose stop and Watchtower redeploy both wait 60s for the api
    (uvicorn 10s drain + 35s IB disconnect wait + 10s other cleanup); a 10s default SIGKILLs it with
    its gateway sockets open."""
    import re

    services = yaml.safe_load(_text())["services"]
    assert services["api"]["stop_grace_period"] == "60s"
    assert services["watchtower"]["environment"]["WATCHTOWER_TIMEOUT"] == "60s"
    dev = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())["services"]
    assert dev["api"]["stop_grace_period"] == "60s"
    dockerfile = (REPO_ROOT / "docker" / "api.Dockerfile").read_text()
    drain = int(re.search(r'"--timeout-graceful-shutdown", "(\d+)"', dockerfile).group(1))
    from xenon.api import server

    assert drain + server._SHUTDOWN_IB_DISCONNECT_S + sum(server._SHUTDOWN_STEP_S.values()) < 60
