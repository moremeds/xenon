"""Futu-history sync orchestrator (skeleton — Tasks 11-14 fill in the body).

One idempotent function — futu_history_sync(scope, since) — will eventually
drive both first-run backfill and daily-forward UPSERT. Invocation paths:
  - CLI: scripts/futu_history_sync.py (M6)
  - FastAPI: POST /futu/history/sync (M7)
  - launchd: 16:30 ET daily (M6)

Singleflight via pg_try_advisory_lock(LOCK_KEY_FUTU_HISTORY_SYNC = 7343001).
A second concurrent invocation returns status='in_progress' instead of
contending — only one worker actually pulls.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from xenon.api.services.advisory_lock import (
    LOCK_KEY_FUTU_HISTORY_SYNC,
    try_advisory_lock,
)
from xenon.db.engine import get_sync_engine
from xenon.execution.account_scope import AccountScope

log = logging.getLogger(__name__)

MIN_HISTORICAL_DATE = date(2024, 5, 1)


@dataclass
class SinceUnparseable(Exception):
    reason_code: str = "since_unparseable"
    bad_value: str = ""


@dataclass
class SinceOutOfRange(Exception):
    reason_code: str = "since_out_of_range"
    bad_value: str = ""


def parse_since(since: str | None) -> date | None:
    """Return the start date or None for 'auto'. Raises structured errors.

    Upper bound uses America/New_York date — UTC would let `since=tomorrow_ET`
    slip through between 19:00 ET and 04:00 ET on the boundary day.
    """
    from zoneinfo import ZoneInfo

    if since is None or since == "auto":
        return None
    try:
        d = date.fromisoformat(since)
    except ValueError as exc:
        raise SinceUnparseable(bad_value=since) from exc
    today_et = datetime.now(ZoneInfo("America/New_York")).date()
    if d < MIN_HISTORICAL_DATE or d > today_et:
        raise SinceOutOfRange(bad_value=since)
    return d


def futu_history_sync(
    scope: AccountScope,
    since: date | None,
    rematch: bool = False,
    until_date: date | None = None,
) -> dict[str, Any]:
    """Idempotent orchestrator. Returns a JSON-serializable result dict.

    NOTE (Task 10): the body is intentionally a no-op placeholder. Tasks
    11-14 will fill in the pull/rematch/snapshot/NAV-walk steps. The skeleton
    is committed now so M6 CLI + M7 API can wire against the public surface.
    """
    eng = get_sync_engine()
    with eng.connect() as conn:
        with try_advisory_lock(conn, LOCK_KEY_FUTU_HISTORY_SYNC) as locked:
            if not locked:
                log.info("futu_history_sync: another run holds the lock; skipping")
                return {
                    "status": "in_progress",
                    "fills_added": 0,
                    "cash_flows_added": 0,
                    "nav_rows_added": 0,
                    "warnings": [],
                }
            return _run_sync_under_lock(
                scope=scope,
                since=since,
                rematch=rematch,
                until_date=until_date,
            )


def _run_sync_under_lock(
    scope: AccountScope,
    since: date | None,
    rematch: bool,
    until_date: date | None,
) -> dict[str, Any]:
    """Placeholder for the body — filled by Tasks 11-14."""
    log.info(
        "futu_history_sync placeholder run scope=%s since=%s rematch=%s until=%s",
        scope,
        since,
        rematch,
        until_date,
    )
    return {
        "status": "ok",
        "fills_added": 0,
        "cash_flows_added": 0,
        "nav_rows_added": 0,
        "warnings": ["not_implemented_placeholder_task_10_skeleton_only"],
    }
