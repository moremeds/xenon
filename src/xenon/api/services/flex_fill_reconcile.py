"""Backfill externally-placed IB fills from IB Flex into ``xenon.order_fills``.

The live IB pool's ``reqExecutions`` is own-client: with no master API client ID
configured on the Gateway, it only returns executions for orders the pool itself
placed. Fills placed via TWS / IBKR mobile / any other session never reach
``order_fills``, so they're absent from "Today's Executed Orders" and Realized
P&L, and the imported ``snapshot-<permId>`` row stays WORKING after it fills.

IB Flex is account-level and sees every fill. This reconcile pulls Flex
executions, inserts the missing ones via the existing ``record_external_fills``
path (idempotent on ``exec_id`` → never double-counts a live-mirrored fill), then
marks any WORKING/PARTIALLY_FILLED snapshot row whose ``perm_id`` now has covering
fills FILLED. It runs on the Flex cadence (a few times/day), NOT the live pool
path, so it's immune to live-session degradation. Honors ``XENON_READ_ONLY=1``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from decimal import Decimal
from typing import Any, Awaitable, Callable, Optional, Protocol

from sqlalchemy import func, select

from xenon.api.guards import is_read_only
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_fills, order_submissions
from xenon.execution import orders_store
from xenon.execution.account_scope import AccountScope
from xenon.execution.ib_reconcile import record_external_fills

logger = logging.getLogger(__name__)

_RECONCILABLE_STATES = ("WORKING", "PARTIALLY_FILLED")

# Flex statements regenerate only a few times/day and the legacy endpoint
# throttles (ErrorCode 1018) on frequent calls, so reconcile on a slow cadence.
DEFAULT_FLEX_RECONCILE_INTERVAL_S = 1800  # 30 min


class _FlexClient(Protocol):
    def fetch_executions(self, days_back: int = ...) -> list: ...


def _flex_exec_to_record_dict(ex: Any) -> dict[str, Any]:
    """Map a Flex ``Execution`` dataclass to the dict shape ``record_external_fills``
    consumes. Enums are unwrapped to their ``.value`` (``Side.BUY`` → ``"BOT"``,
    which ``_normalize_fill_side`` canonicalizes to ``"BUY"``)."""
    side = getattr(ex.side, "value", None) or str(ex.side)
    sec_type = getattr(ex.sec_type, "value", None) or str(ex.sec_type)
    strike = float(ex.strike) if getattr(ex, "strike", None) is not None else None
    return {
        "exec_id": ex.exec_id,
        "perm_id": ex.perm_id,
        "ib_order_id": ex.ib_order_id,
        "con_id": None,
        "symbol": ex.symbol,
        "side": side,
        "shares": ex.quantity,
        "qty": ex.quantity,
        "price": ex.price,
        "commission": ex.commission,
        "commission_ready": True,  # Flex carries final commission
        "time": ex.time,
        "sec_type": sec_type,
        "strike": strike,
        "right": getattr(ex, "right", None),
        "expiry": getattr(ex, "expiry", None),
    }


def _default_flex_client() -> _FlexClient | None:
    token = os.environ.get("IB_FLEX_TOKEN")
    query_id = os.environ.get("IB_FLEX_QUERY_ID")
    if not token or not query_id:
        return None
    from xenon.trade_blotter.flex_query import FlexQueryClient

    return FlexQueryClient(token, query_id)


def _reconcile_working_snapshots(scope: AccountScope) -> int:
    """Mark WORKING/PARTIALLY_FILLED rows whose perm_id has covering fills FILLED.

    Independent of the open-order snapshot (which the sweep relies on and which is
    empty when the live feed is blind): covering fills alone prove the order filled.
    BAG envelopes are skipped — the leg/envelope quantity ambiguity is the sweep's
    domain and out of scope for a single-leg backfill.
    """
    engine = get_sync_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                order_submissions.c.submission_id,
                order_submissions.c.perm_id,
                order_submissions.c.quantity,
                order_submissions.c.security_type,
            ).where(
                order_submissions.c.broker == scope.broker,
                order_submissions.c.account_env == scope.account_env,
                order_submissions.c.broker_account == scope.broker_account,
                order_submissions.c.state.in_(_RECONCILABLE_STATES),
                order_submissions.c.perm_id.isnot(None),
            )
        ).all()

    reconciled = 0
    for row in rows:
        if str(row.security_type or "").upper() == "BAG":
            continue
        engine_q = get_sync_engine()
        with engine_q.connect() as conn:
            fill_qty, fill_value = conn.execute(
                select(
                    func.coalesce(func.sum(order_fills.c.qty), 0),
                    func.coalesce(func.sum(order_fills.c.qty * order_fills.c.price), 0),
                ).where(
                    order_fills.c.perm_id == str(row.perm_id),
                    order_fills.c.broker == scope.broker,
                    order_fills.c.account_env == scope.account_env,
                    order_fills.c.broker_account == scope.broker_account,
                )
            ).one()
        fill_qty = Decimal(str(fill_qty))
        order_qty = Decimal(str(row.quantity))
        if fill_qty <= 0 or fill_qty < order_qty:
            continue
        avg = (Decimal(str(fill_value)) / fill_qty) if fill_qty else None
        applied = orders_store.mark_terminal(
            submission_id=row.submission_id,
            state="FILLED",
            reason_code=None,
            filled_qty=int(fill_qty),
            avg_fill_price=avg,
            expected_states=_RECONCILABLE_STATES,
        )
        if applied:
            orders_store.record_event(
                row.submission_id,
                "RECONCILED",
                {"source": "flex_reconcile", "filled_qty": str(fill_qty)},
            )
            reconciled += 1
    return reconciled


def reconcile_flex_fills(
    *,
    scope: AccountScope,
    flex_client: _FlexClient | None = None,
    days_back: int = 7,
) -> dict[str, Any]:
    """Pull Flex executions, backfill missing order_fills, reconcile WORKING rows.

    Returns a result dict: ``record_external_fills`` counters plus
    ``flex_executions`` and ``reconciled``; or ``{"skipped": <reason>}``.
    """
    if is_read_only():
        return {"skipped": "read_only"}

    client = flex_client or _default_flex_client()
    if client is None:
        return {"skipped": "flex_not_configured"}

    try:
        execs = client.fetch_executions(days_back=days_back)
    except Exception as exc:  # noqa: BLE001
        logger.warning("flex_fill_reconcile: fetch_executions failed: %s", exc)
        return {"error": str(exc)}

    dicts = [_flex_exec_to_record_dict(e) for e in execs]
    result = record_external_fills(dicts, scope=scope)
    reconciled = _reconcile_working_snapshots(scope)
    logger.info(
        "flex_fill_reconcile: scope=%s flex_execs=%d inserted=%d reconciled=%d",
        scope.as_dict(),
        len(execs),
        result.get("inserted", 0),
        reconciled,
    )
    return {"flex_executions": len(execs), "reconciled": reconciled, **result}


async def _default_async_runner(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Run the blocking reconcile (Flex HTTP + sync DB) off the event loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


async def flex_reconcile_loop(
    *,
    scope: AccountScope,
    interval_s: float = DEFAULT_FLEX_RECONCILE_INTERVAL_S,
    days_back: int = 7,
    async_runner: Optional[Callable[..., Awaitable[Any]]] = None,
) -> None:
    """Forever-loop wrapper: backfill Flex fills + reconcile WORKING rows on a slow
    cadence. First tick runs immediately at startup. Survives single-tick failures
    (logged + slept off). Exits cleanly on CancelledError from lifespan shutdown."""
    runner = async_runner or _default_async_runner
    logger.info("flex_fill_reconcile: loop starting scope=%s interval=%ss", scope.as_dict(), interval_s)
    while True:
        try:
            result = await runner(reconcile_flex_fills, scope=scope, days_back=days_back)
            logger.info("flex_fill_reconcile tick: %s", result)
        except asyncio.CancelledError:
            logger.info("flex_fill_reconcile: loop cancelled, exiting")
            raise
        except Exception:  # noqa: BLE001
            logger.exception("flex_fill_reconcile: loop tick raised unexpectedly")
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            logger.info("flex_fill_reconcile: loop cancelled during sleep, exiting")
            raise
