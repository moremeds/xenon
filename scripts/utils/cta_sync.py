"""Helpers for scheduling and hardening MenthorQ CTA cache refreshes."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from zoneinfo import ZoneInfo

from utils.market_calendar import _is_trading_day


ET = ZoneInfo("America/New_York")
MARKET_CLOSE_MINUTE = 16 * 60
CTA_SYNC_ET_SLOTS = [
    (16, 15),
    (17, 0),
]

PROJECT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_DIR / "data"
CTA_CACHE_DIR = DATA_DIR / "menthorq_cache"
CTA_SERVICE_HEALTH_DIR = DATA_DIR / "service_health"
CTA_SYNC_STATUS_PATH = CTA_SERVICE_HEALTH_DIR / "cta-sync.json"
CTA_SYNC_HISTORY_PATH = CTA_SERVICE_HEALTH_DIR / "cta-sync-history.jsonl"
CTA_SYNC_LOCK_DIR = CTA_SERVICE_HEALTH_DIR / "cta-sync.lock"
CTA_SYNC_ARTIFACT_DIR = PROJECT_DIR / "logs" / "cta-sync-artifacts"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = (15, 45)


def latest_closed_trading_day(now: datetime | None = None) -> str:
    """Return the latest trading day whose CTA cache should exist."""

    now_et = _coerce_et(now)
    candidate = now_et.date()
    minutes = now_et.hour * 60 + now_et.minute

    if not (_is_trading_day(_to_naive(candidate)) and minutes >= MARKET_CLOSE_MINUTE):
        candidate -= timedelta(days=1)

    while not _is_trading_day(_to_naive(candidate)):
        candidate -= timedelta(days=1)

    return candidate.isoformat()


def build_sync_health_record(
    *,
    target_date: str,
    status: str,
    trigger: str,
    started_at: str,
    finished_at: str | None,
    duration_ms: int | None,
    attempt_count: int,
    cache_path: str | None,
    error_type: str | None,
    error_excerpt: str | None,
    artifact_log_path: str | None,
) -> dict[str, Any]:
    return {
        "service": "cta-sync",
        "status": status,
        "trigger": trigger,
        "target_date": target_date,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
        "attempt_count": attempt_count,
        "cache_path": cache_path,
        "error_type": error_type,
        "error_excerpt": error_excerpt,
        "artifact_log_path": artifact_log_path,
    }


def write_sync_health_record(
    record: dict[str, Any],
    *,
    status_path: Path = CTA_SYNC_STATUS_PATH,
    history_path: Path | None = CTA_SYNC_HISTORY_PATH,
) -> None:
    status_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, indent=2) + "\n"
    tmp_path = status_path.with_suffix(f"{status_path.suffix}.tmp")
    tmp_path.write_text(payload)
    tmp_path.replace(status_path)

    if history_path is not None:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with history_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


def classify_sync_error(output: str) -> str:
    text = _sanitize_excerpt(output, max_len=600).lower()

    if "username or password" in text or "still on login page" in text or "login failed" in text:
        return "auth_rejected"
    if "cloudflare" in text or "captcha" in text or "challenge" in text:
        return "challenge_page"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "form fields not found" in text or "submit control not found" in text or "selector" in text:
        return "selector_failure"
    if "no menthorq data retrieved" in text or "no cta data extracted" in text or "returned no data" in text:
        return "empty_payload"
    return "unknown"


def run_cta_sync(
    *,
    project_dir: Path = PROJECT_DIR,
    python_bin: str = "python3",
    now: datetime | None = None,
    trigger: str = "manual",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_seconds: Sequence[int] = DEFAULT_BACKOFF_SECONDS,
    runner: Callable[..., Any] = subprocess.run,
    sleep_fn: Callable[[int], Any] = time.sleep,
) -> int:
    project_dir = Path(project_dir)
    cache_dir = project_dir / "data" / "menthorq_cache"
    health_dir = project_dir / "data" / "service_health"
    status_path = health_dir / "cta-sync.json"
    history_path = health_dir / "cta-sync-history.jsonl"
    lock_dir = health_dir / "cta-sync.lock"
    artifacts_dir = project_dir / "logs" / "cta-sync-artifacts"
    target_date = latest_closed_trading_day(now)
    cache_path = cache_dir / f"cta_{target_date}.json"
    started = _utc_now_iso()

    if not _try_acquire_lock(lock_dir):
        return 0

    write_sync_health_record(
        build_sync_health_record(
            target_date=target_date,
            status="running",
            trigger=trigger,
            started_at=started,
            finished_at=None,
            duration_ms=None,
            attempt_count=0,
            cache_path=None,
            error_type=None,
            error_excerpt=None,
            artifact_log_path=None,
        ),
        status_path=status_path,
        history_path=history_path,
    )

    try:
        if cache_path.exists():
            finished = _utc_now_iso()
            duration_ms = _duration_ms(started, finished)
            write_sync_health_record(
                build_sync_health_record(
                    target_date=target_date,
                    status="success",
                    trigger=trigger,
                    started_at=started,
                    finished_at=finished,
                    duration_ms=duration_ms,
                    attempt_count=0,
                    cache_path=_relative_path(cache_path, project_dir),
                    error_type=None,
                    error_excerpt=None,
                    artifact_log_path=None,
                ),
                status_path=status_path,
                history_path=history_path,
            )
            return 0

        attempt_count = 0
        last_excerpt: str | None = None
        last_error_type: str | None = None
        last_artifact_path: str | None = None

        for index in range(max_attempts):
            attempt_count = index + 1
            command = [python_bin, "scripts/fetch_menthorq_cta.py", "--date", target_date]

            try:
                result = runner(
                    command,
                    cwd=str(project_dir),
                    capture_output=True,
                    text=True,
                )
            except Exception as exc:
                result = subprocess.CompletedProcess(
                    command,
                    returncode=1,
                    stdout="",
                    stderr=str(exc),
                )

            artifact_path = _write_attempt_artifact(
                artifacts_dir=artifacts_dir,
                project_dir=project_dir,
                target_date=target_date,
                attempt_number=attempt_count,
                command=command,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
            )
            last_artifact_path = artifact_path

            if result.returncode == 0 and cache_path.exists():
                finished = _utc_now_iso()
                duration_ms = _duration_ms(started, finished)
                write_sync_health_record(
                    build_sync_health_record(
                        target_date=target_date,
                        status="success",
                        trigger=trigger,
                        started_at=started,
                        finished_at=finished,
                        duration_ms=duration_ms,
                        attempt_count=attempt_count,
                        cache_path=_relative_path(cache_path, project_dir),
                        error_type=None,
                        error_excerpt=None,
                        artifact_log_path=None,
                    ),
                    status_path=status_path,
                    history_path=history_path,
                )
                return 0

            combined = "\n".join(
                part for part in [result.stderr or "", result.stdout or ""] if part
            ) or f"CTA sync command exited with {result.returncode}"
            last_excerpt = _sanitize_excerpt(combined)
            last_error_type = classify_sync_error(combined)

            if index < max_attempts - 1:
                wait_seconds = backoff_seconds[min(index, len(backoff_seconds) - 1)] if backoff_seconds else 0
                if wait_seconds > 0:
                    sleep_fn(wait_seconds)

        finished = _utc_now_iso()
        duration_ms = _duration_ms(started, finished)
        write_sync_health_record(
            build_sync_health_record(
                target_date=target_date,
                status="error",
                trigger=trigger,
                started_at=started,
                finished_at=finished,
                duration_ms=duration_ms,
                attempt_count=attempt_count,
                cache_path=None,
                error_type=last_error_type,
                error_excerpt=last_excerpt,
                artifact_log_path=last_artifact_path,
            ),
            status_path=status_path,
            history_path=history_path,
        )
        return 1
    finally:
        _release_lock(lock_dir)


def _coerce_et(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(ET)
    if now.tzinfo is None:
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def _to_naive(value: date) -> datetime:
    return datetime(value.year, value.month, value.day, 12, 0, 0)


def _sanitize_excerpt(text: str, *, max_len: int = 220) -> str:
    if not isinstance(text, str):
        return ""
    collapsed = " ".join(text.split())
    return collapsed[:max_len]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _duration_ms(started_at: str, finished_at: str) -> int:
    started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    finished = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    return max(0, int((finished - started).total_seconds() * 1000))


def _relative_path(path: Path, project_dir: Path) -> str:
    try:
        return str(path.relative_to(project_dir))
    except ValueError:
        return str(path)


def _try_acquire_lock(lock_dir: Path) -> bool:
    try:
        lock_dir.mkdir(parents=True, exist_ok=False)
        return True
    except FileExistsError:
        return False


def _release_lock(lock_dir: Path) -> None:
    try:
        lock_dir.rmdir()
    except FileNotFoundError:
        return
    except OSError:
        return


def _write_attempt_artifact(
    *,
    artifacts_dir: Path,
    project_dir: Path,
    target_date: str,
    attempt_number: int,
    command: Sequence[str],
    stdout: str,
    stderr: str,
) -> str:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = artifacts_dir / f"cta-sync-{target_date}-attempt-{attempt_number}-{timestamp}.log"
    path.write_text(
        "\n".join(
            [
                f"command: {' '.join(command)}",
                "",
                "[stdout]",
                stdout.strip(),
                "",
                "[stderr]",
                stderr.strip(),
                "",
            ]
        )
    )
    return _relative_path(path, project_dir)
