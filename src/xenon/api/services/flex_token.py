"""IB Flex Web Service token expiry for ``GET /health`` (spec §2.9).

Replaces the monitor daemon's ``flex_token_check`` handler. Same source
(``data/flex_token_config.json``, operator-maintained config, not runtime
order data) and the same thresholds (``reminder_days``, default
30/14/7/1 days). Read-only: unlike the daemon it never writes
``reminders_sent`` back; the Operator console shows the state continuously.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_REMINDER_DAYS = (30, 14, 7, 1)
CONFIG_PATH = Path(__file__).resolve().parents[4] / "data" / "flex_token_config.json"


def flex_token_status(path: Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    cfg_path = path or CONFIG_PATH
    try:
        config = json.loads(cfg_path.read_text())
        expires_str = config["expires_at"]
        expires_at = datetime.fromisoformat(expires_str)
    except FileNotFoundError:
        return {"configured": False}
    except Exception:  # noqa: BLE001 — bad config must not break /health
        logger.warning("[health] invalid flex token config at %s", cfg_path, exc_info=True)
        return {"configured": False, "error": "invalid flex_token_config"}
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    days_remaining = (expires_at.astimezone(timezone.utc) - current).days
    reminder_days = sorted(
        (int(d) for d in config.get("reminder_days") or DEFAULT_REMINDER_DAYS),
        reverse=True,
    )
    active = next((d for d in reversed(reminder_days) if days_remaining <= d), None)
    return {
        "configured": True,
        "expires_at": expires_str,
        "days_remaining": days_remaining,
        "reminder_days": reminder_days,
        "active_threshold": active,
        "should_warn": active is not None,
        "expired": days_remaining <= 0,
    }
