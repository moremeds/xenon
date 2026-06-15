"""Best-effort heartbeat writer for the Operator console (Tier B).

Background loops/CLIs call :func:`record_service_health` to record a per-
``(service, AccountScope)`` heartbeat into ``xenon.service_health``. The
Operator console reads these rows to surface writer freshness.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Module-level import so tests can monkeypatch the engine factory here.
from xenon.db.engine import get_sync_engine  # noqa: E402


def _resolve_scope(
    broker: Optional[str],
    account_env: Optional[str],
    broker_account: Optional[str],
) -> tuple[str, str, str]:
    """Resolve AccountScope columns. Explicit args win; else read the env vars
    sync subprocesses already set (``XENON_TRADING_MODE`` / ``XENON_BROKER_ACCOUNT``,
    per src/xenon/CLAUDE.md § Broker Account Scope). Falls back to ``unknown`` so
    a heartbeat is never silently dropped for a missing env."""
    return (
        broker or "IB",
        account_env or os.environ.get("XENON_TRADING_MODE") or "unknown",
        broker_account or os.environ.get("XENON_BROKER_ACCOUNT") or "unknown",
    )


def record_service_health(
    service: str,
    state: str = "ok",
    *,
    broker: Optional[str] = None,
    account_env: Optional[str] = None,
    broker_account: Optional[str] = None,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
    detail: Optional[str] = None,
    error: Optional[dict[str, Any] | str] = None,
) -> None:
    """Best-effort upsert of a per-``(service, scope)`` heartbeat.

    - Scope (broker/account_env/broker_account) is required by the repo rule.
      Callers with an AccountScope pass it explicitly; subprocess CLIs let it
      resolve from the env (see :func:`_resolve_scope`).
    - Never raises: a heartbeat failure must not break the caller's loop.
    - No-ops under ``XENON_READ_ONLY=1`` (mirrors ``_save_portfolio_to_postgres``).
    """
    if os.environ.get("XENON_READ_ONLY") == "1":
        return
    try:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from xenon.db.schema import service_health

        brk, env, acct = _resolve_scope(broker, account_env, broker_account)
        now = datetime.now(timezone.utc)
        err_text = json.dumps(error) if isinstance(error, dict) else error
        values = dict(
            service=service,
            broker=brk,
            account_env=env,
            broker_account=acct,
            state=state,
            detail=detail,
            last_error=err_text,
            last_started_at=started_at,
            last_finished_at=finished_at,
            updated_at=now,
        )
        engine = get_sync_engine()
        with engine.begin() as conn:
            stmt = pg_insert(service_health).values(**values)
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    service_health.c.service,
                    service_health.c.broker,
                    service_health.c.account_env,
                    service_health.c.broker_account,
                ],
                set_={
                    k: stmt.excluded[k]
                    for k in values
                    if k not in ("service", "broker", "account_env", "broker_account")
                },
            )
            conn.execute(stmt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("record_service_health(%s) failed: %s", service, exc)
