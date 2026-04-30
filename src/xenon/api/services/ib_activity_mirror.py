"""IB→Postgres activity mirror.

Symmetric counterpart to ``register_from_snapshot`` (open orders): pulls
fills/executions from IB and inserts them into ``xenon.order_fills`` so
the blotter sees TWS-side activity even when an order originated outside
Xenon (manually placed in TWS, modified in TWS, etc).

Phase 1 surfaces a single boot-time replay. The periodic poller (Phase 2)
will reuse the same internals.

Why best-effort
---------------
A failure here must never block FastAPI from serving. Boot-time replay
of fills is convenience, not correctness — the open-order import is the
correctness path. The blotter can always be reconciled later by running
``xenon-ib-reconcile`` manually. Therefore every public entry point in
this module catches and logs.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from xenon.execution.account_scope import AccountScope
from xenon.execution.ib_orders import fetch_open_orders as _fetch_open_orders
from xenon.execution.ib_orders import sync_open_orders_to_postgres as _sync_open_orders_to_postgres
from xenon.execution.ib_reconcile import fetch_ib_executions as _fetch_ib_executions
from xenon.execution.ib_reconcile import record_external_fills as _record_external_fills

logger = logging.getLogger(__name__)

# Default poll cadence. Override per-process via XENON_IB_ACTIVITY_POLL_S.
# Chosen to be the same order of magnitude as the open-order import — a
# user noticing a stale price/qty in the UI should not have to wait > 1
# minute for the mirror to catch up.
DEFAULT_POLL_INTERVAL_S = 60


def reconcile_fills_on_boot(
    *,
    ib_client_factory: Callable[[], Any],
    scope: AccountScope,
    lookback_days: int = 7,
) -> dict:
    """Pull recent IB executions once and insert them into xenon.order_fills.

    Parameters mirror ``_run_rehydrate_on_boot``: a factory so we can reuse
    the long-lived IB pool's sync client without re-establishing a TCP
    connection per call.

    Returns a result dict with one of the following shapes:
        {"skipped": True, "reason": "..."}     — factory raised
        {"error": "..."}                       — fetch raised
        {"inserted": N, "replayed": M, ...}    — ran cleanly
    """
    try:
        client = ib_client_factory()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ib_activity_mirror: client factory failed: %s", exc)
        return {"skipped": True, "reason": str(exc)}

    try:
        executions = _fetch_ib_executions(client, lookback_days=lookback_days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ib_activity_mirror: fetch_ib_executions failed: %s", exc)
        return {"error": str(exc)}

    try:
        result = _record_external_fills(executions, scope=scope)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ib_activity_mirror: record_external_fills failed: %s", exc)
        return {"error": str(exc)}

    logger.info(
        "ib_activity_mirror: replayed %d fills (inserted=%d, replayed=%d, groups=%d)",
        len(executions),
        result.get("inserted", 0),
        result.get("replayed", 0),
        len(result.get("affected_legacy_ids") or []),
    )
    return result


def _safe_open_orders_tick(client: Any, *, scope: AccountScope) -> dict:
    """Best-effort: fetch + sync open orders. Catches & returns instead of raising."""
    try:
        open_orders = _fetch_open_orders(client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ib_activity_mirror tick: fetch_open_orders failed: %s", exc)
        return {"error": str(exc)}
    try:
        return _sync_open_orders_to_postgres(open_orders, scope=scope)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ib_activity_mirror tick: sync_open_orders_to_postgres failed: %s", exc)
        return {"error": str(exc)}


def _safe_fills_tick(client: Any, *, scope: AccountScope, lookback_days: int) -> dict:
    """Best-effort: fetch + record fills. Catches & returns instead of raising."""
    try:
        executions = _fetch_ib_executions(client, lookback_days=lookback_days)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ib_activity_mirror tick: fetch_ib_executions failed: %s", exc)
        return {"error": str(exc)}
    try:
        return _record_external_fills(executions, scope=scope)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ib_activity_mirror tick: record_external_fills failed: %s", exc)
        return {"error": str(exc)}


def run_activity_poll_tick(
    *,
    ib_client_factory: Callable[[], Any],
    scope: AccountScope,
    lookback_days: int = 7,
) -> dict:
    """Run one IB→Postgres mirror tick: open orders + fills.

    The two surfaces are independent — a transient failure on one must not
    skip the other (otherwise the bug we just fixed has a sibling: open-order
    drift hides a fresh fill, or a hung get_fills() call leaves the order
    list stuck on the prior tick's state). Each side has its own try/except.
    """
    try:
        client = ib_client_factory()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ib_activity_mirror tick: client factory failed: %s", exc)
        return {
            "open_orders": {"skipped": True, "reason": str(exc)},
            "fills": {"skipped": True, "reason": str(exc)},
        }

    return {
        "open_orders": _safe_open_orders_tick(client, scope=scope),
        "fills": _safe_fills_tick(client, scope=scope, lookback_days=lookback_days),
    }


async def activity_poller_loop(
    *,
    ib_client_factory: Callable[[], Any],
    scope: AccountScope,
    interval_s: float = DEFAULT_POLL_INTERVAL_S,
    lookback_days: int = 7,
) -> None:
    """Forever-loop wrapper around run_activity_poll_tick.

    Called from the FastAPI lifespan as a background task. Survives any
    single-tick failure (logged + slept off). Exits cleanly on
    ``asyncio.CancelledError`` from the lifespan shutdown path.
    """
    logger.info(
        "ib_activity_mirror: poller starting for scope=%s interval=%ss",
        scope.as_dict(),
        interval_s,
    )
    while True:
        try:
            result = await asyncio.to_thread(
                run_activity_poll_tick,
                ib_client_factory=ib_client_factory,
                scope=scope,
                lookback_days=lookback_days,
            )
            oo = result.get("open_orders") or {}
            fills = result.get("fills") or {}
            logger.info(
                "ib_activity_mirror tick: open_orders[reg=%s upd=%s skip=%s] fills[ins=%s upd=%s rep=%s]",
                oo.get("registered"),
                oo.get("updated"),
                oo.get("skipped"),
                fills.get("inserted"),
                fills.get("updated"),
                fills.get("replayed"),
            )
        except asyncio.CancelledError:
            logger.info("ib_activity_mirror: poller cancelled, exiting")
            raise
        except Exception:  # noqa: BLE001
            # to_thread should not raise — run_activity_poll_tick catches —
            # but if a programmer adds an un-caught path, we still don't want
            # the loop to die.
            logger.exception("ib_activity_mirror: poller tick raised unexpectedly")

        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            logger.info("ib_activity_mirror: poller cancelled during sleep, exiting")
            raise
