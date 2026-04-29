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

import logging
from typing import Any, Callable

from xenon.execution.account_scope import AccountScope
from xenon.execution.ib_reconcile import fetch_ib_executions as _fetch_ib_executions
from xenon.execution.ib_reconcile import record_external_fills as _record_external_fills

logger = logging.getLogger(__name__)


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
