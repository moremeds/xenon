"""Risk guard wiring into the api process: IO edges, start / stop (spec §5.4).

Started from the FastAPI lifespan. It starts even when the account scope is
unresolved (the env scope is the fallback for reads, and SCOPE_UNRESOLVED is
raised as a BLIND cause), because an unresolved scope is exactly when the
operator most needs to know the guard is blind.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any

from xenon.api.services.risk_guard import config as rg_config
from xenon.api.services.risk_guard.account import load_drawdown_basis
from xenon.api.services.risk_guard.notify import Notifier, NotifyConfig
from xenon.api.services.risk_guard.service import RiskGuard, run_loop
from xenon.api.services.risk_guard.sources import IBQuoteFeed

logger = logging.getLogger(__name__)


def resolve_scope(app_state: Any) -> tuple[Any, bool]:
    """(scope, verified). Verified = app.state scope with ``mode_verified``; else the env scope."""
    from xenon.execution.account_scope import resolve_from_app_state, resolve_from_env

    try:
        scope = resolve_from_app_state(app_state)
        return scope, getattr(app_state, "mode_verified", False) is True
    except ValueError:
        pass
    try:
        return resolve_from_env(), False
    except ValueError:
        return None, False


def system_health(wall: datetime) -> dict[str, str]:
    """BLIND causes from the api process itself (order path, IB connection, scope)."""
    from xenon.api import server

    causes: dict[str, str] = {}
    owner = server.order_owner
    if owner is not None and owner.halted:
        causes["OWNER_HALTED"] = f"orders halted: {owner.status().get('halt_reason')}"
    pool = server.ib_pool
    if pool is None or not pool.is_connected("data"):
        causes["DATA_ROLE_DOWN"] = "IB data connection down (gateway closed or not connected)"
    elif pool.upstream_status().get("state") == "lost":
        causes["UPSTREAM_LOST"] = "IB Gateway lost the IB servers (1100/2110)"
    _scope, verified = resolve_scope(server.app.state)
    if not verified:
        causes["SCOPE_UNRESOLVED"] = "account scope not verified; account checks use the env scope"
    return causes


def build() -> RiskGuard:
    from xenon.api import server
    from xenon.api.guards import is_read_only
    from xenon.db.engine import get_engine
    from xenon.db.queries import risk_guard as q

    notifier = Notifier(NotifyConfig.from_env())
    beat_url = os.environ.get("XENON_RISK_HEARTBEAT_URL", "").strip()

    async def heartbeat(payload: dict) -> bool:
        return await notifier._post("heartbeat", beat_url, json=payload) == "sent"

    return RiskGuard(
        feed=IBQuoteFeed(server.ib_pool),
        scope_fn=lambda: resolve_scope(server.app.state)[0],
        snapshot_fn=lambda scope: q.latest_snapshot(get_engine(), scope),
        basis_fn=lambda scope, today: load_drawdown_basis(get_engine(), scope, today),
        record_fn=lambda scope, **kw: q.record_event(get_engine(), scope, **kw),
        restore_fn=lambda scope, since: q.latest_ack_state(get_engine(), scope, since),
        health_fn=system_health,
        notifier=notifier,
        heartbeat_fn=heartbeat if beat_url else None,
        read_only_fn=is_read_only,
    )


def maybe_start() -> None:
    """Start the loop unless test mode or ``XENON_RISK_GUARD`` env is off.

    The env flag is the deploy switch; the DB toggle (``/admin/settings``) can
    turn a started guard off and on again without a restart.
    """
    from xenon.api import server

    app = server.app
    app.state.risk_guard, app.state.risk_guard_task = None, None
    if server._is_test_mode():
        logger.info("test_mode: skipping risk guard")
        return
    if not rg_config.env_enabled():
        logger.info("risk guard off (XENON_RISK_GUARD not set)")
        return
    guard = build()
    cfg = guard.notifier.cfg
    if not (cfg.push_on or cfg.email_on):
        logger.error("risk guard started with no notification channel (XENON_RISK_NTFY_URL / XENON_RISK_RESEND_TOKEN)")
    app.state.risk_guard = guard
    app.state.risk_guard_task = asyncio.create_task(run_loop(guard))
    logger.info(
        "risk guard started: push=%s email=%s heartbeat=%s", cfg.push_on, cfg.email_on, guard.heartbeat_fn is not None
    )


async def stop(app: Any) -> None:
    task, guard = getattr(app.state, "risk_guard_task", None), getattr(app.state, "risk_guard", None)
    app.state.risk_guard_task = None
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    if guard is not None:
        await guard.drain()
        await guard.notifier.aclose()
