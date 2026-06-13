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
from typing import Any, Awaitable, Callable, Optional

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


TWS_CANCEL_REASON = "TWS_CANCEL_MIRROR"

# submission_ids that were missing from the open-order snapshot on the
# previous sweep. One-tick grace: an order that fills mid-tick disappears
# before its fill row lands; cancelling on first disappearance would
# misclassify it. Module-level on purpose — survives across poller ticks
# within one FastAPI process; a restart just waits one extra tick.
_SWEEP_GRACE: set[str] = set()


def sweep_disappeared_orders(
    open_orders: list[dict],
    *,
    scope: AccountScope,
    grace: set[str] | None = None,
) -> dict:
    """Transition WORKING/PARTIALLY_FILLED rows that vanished from IB's
    open-order snapshot to FILLED (fills cover quantity) or CANCELLED
    (missing two consecutive sweeps). Returns counters for the tick log.
    """
    from decimal import Decimal

    from sqlalchemy import func, select

    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import order_fills, order_submissions
    from xenon.execution import orders_store

    tracked = _SWEEP_GRACE if grace is None else grace
    # Match IB's identity logic in sync_open_orders_to_postgres: a BAG
    # fetched from a non-originating client has orderId=0 and is keyed by
    # permId; a fresh order has permId=0 until the openOrder ack (the
    # documented permId=0 race) and is keyed by orderId. An order is
    # "present" if EITHER its perm_id OR its ib_order_id appears in the
    # snapshot — otherwise the permId=0 race would mark live orders as
    # disappeared and cancel them.
    open_perm_ids = {str(o.get("permId")) for o in open_orders if o.get("permId")}
    open_order_ids = {str(o.get("orderId")) for o in open_orders if o.get("orderId")}

    engine = get_sync_engine()
    with engine.connect() as conn:
        rows = (
            conn.execute(
                select(
                    order_submissions.c.submission_id,
                    order_submissions.c.perm_id,
                    order_submissions.c.ib_order_id,
                    order_submissions.c.quantity,
                    order_submissions.c.security_type,
                ).where(
                    order_submissions.c.state.in_(("WORKING", "PARTIALLY_FILLED")),
                    order_submissions.c.perm_id.isnot(None),
                    order_submissions.c.broker == scope.broker,
                    order_submissions.c.account_env == scope.account_env,
                    order_submissions.c.broker_account == scope.broker_account,
                )
            )
            .mappings()
            .all()
        )

    # Safety against the production Gateway-bounce failure mode: an empty
    # snapshot while WORKING rows exist is far more likely a stale/
    # post-reconnect read than every order vanishing at once. Skip the
    # whole sweep — never mass-cancel on an empty snapshot. (Cost: a TWS
    # cancel of your *only* open order isn't mirrored until the next
    # non-empty snapshot or boot rehydrate — acceptable vs mass-cancel.)
    if not open_orders and rows:
        logger.warning(
            "cancel_sweep: empty open-order snapshot with %d working row(s) — skipping sweep",
            len(rows),
        )
        return {"filled": 0, "cancelled": 0, "graced": 0, "skipped": "empty_snapshot"}

    filled = cancelled = graced = 0
    missing_now: set[str] = set()

    for row in rows:
        sid = row["submission_id"]
        present = str(row["perm_id"]) in open_perm_ids or (
            row["ib_order_id"] and str(row["ib_order_id"]) in open_order_ids
        )
        if present:
            tracked.discard(sid)
            continue

        is_bag = row["security_type"] == "BAG"
        with engine.connect() as conn:
            scope_where = (
                order_fills.c.perm_id == str(row["perm_id"]),
                order_fills.c.broker == scope.broker,
                order_fills.c.account_env == scope.account_env,
                order_fills.c.broker_account == scope.broker_account,
            )
            q = select(
                func.coalesce(func.sum(order_fills.c.qty), 0),
                func.coalesce(func.sum(order_fills.c.qty * order_fills.c.price), 0),
            ).where(*scope_where)
            if is_bag:
                # Per-leg rows duplicate the envelope economically; count
                # only the envelope fill against the combo quantity.
                q = q.where(order_fills.c.metadata["sec_type"].astext == "BAG")
            fill_qty, fill_value = conn.execute(q).one()
            # For a BAG we must distinguish "no fills at all" (genuine
            # cancel candidate) from "leg fills exist but no envelope row"
            # (ambiguous — IB didn't emit a combo-level execution). The
            # latter must NOT be auto-cancelled: a filled combo whose
            # envelope we can't read would be wrongly killed.
            any_fill = False
            if is_bag:
                any_fill = bool(
                    conn.execute(select(func.count()).select_from(order_fills).where(*scope_where)).scalar()
                )

        fill_qty = Decimal(str(fill_qty or 0))
        if is_bag and fill_qty == 0 and any_fill:
            # Ambiguous combo: leg fills present, envelope absent. Stay
            # WORKING, hold in grace, and log — favour never wrongly
            # cancelling a filled combo over closing the gap fast.
            logger.warning(
                "cancel_sweep: BAG %s has leg fills but no envelope row — skipping cancel",
                sid,
            )
            missing_now.add(sid)
            graced += 1
            continue
        order_qty = Decimal(str(row["quantity"]))
        # `fill_qty > 0` guard: a quantity-0 working row (e.g. a fractional
        # open order truncated by the Integer order_submissions.quantity
        # column — see Task 5's note) must never be marked FILLED on zero
        # fills (0 >= 0 would otherwise be True).
        if fill_qty > 0 and fill_qty >= order_qty:
            avg = (Decimal(str(fill_value)) / fill_qty) if fill_qty else None
            orders_store.mark_terminal(
                submission_id=sid,
                state="FILLED",
                reason_code=None,
                filled_qty=int(fill_qty),
                avg_fill_price=avg,
            )
            orders_store.record_event(sid, "RECONCILED", {"source": "cancel_sweep", "filled_qty": str(fill_qty)})
            tracked.discard(sid)
            filled += 1
        elif sid in tracked:
            orders_store.mark_terminal(
                submission_id=sid,
                state="CANCELLED",
                reason_code=TWS_CANCEL_REASON,
                filled_qty=int(fill_qty),
                avg_fill_price=None,
            )
            orders_store.record_event(sid, TWS_CANCEL_REASON, {"source": "cancel_sweep"})
            tracked.discard(sid)
            cancelled += 1
        else:
            missing_now.add(sid)
            graced += 1

    # Grace = exactly the ids missing on THIS sweep. Reappeared/filled/
    # cancelled ids were discarded in the loop; stale ids from prior sweeps
    # (orders that left WORKING by another path, e.g. user cancel) are
    # dropped by the clear(). NOTE: module-global _SWEEP_GRACE is shared
    # process-wide — safe today (one scope per process). If a process ever
    # polls multiple scopes, key the grace by scope.
    tracked.clear()
    tracked.update(missing_now)
    return {"filled": filled, "cancelled": cancelled, "graced": graced}


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

    try:
        open_orders = _fetch_open_orders(client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ib_activity_mirror tick: fetch_open_orders failed: %s", exc)
        open_orders = None

    if open_orders is None:
        oo_result: dict = {"error": "fetch_open_orders failed"}
    else:
        try:
            oo_result = _sync_open_orders_to_postgres(open_orders, scope=scope)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ib_activity_mirror tick: sync_open_orders_to_postgres failed: %s", exc)
            oo_result = {"error": str(exc)}

    fills_result = _safe_fills_tick(client, scope=scope, lookback_days=lookback_days)

    # Sweep only when BOTH feeds succeeded this tick — a failed open-order
    # fetch would otherwise mass-cancel everything, and missing fills data
    # would misclassify mid-tick fills as cancels.
    if open_orders is not None and "error" not in fills_result:
        try:
            sweep_result = sweep_disappeared_orders(open_orders, scope=scope)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ib_activity_mirror tick: cancel sweep failed: %s", exc)
            sweep_result = {"error": str(exc)}
    else:
        sweep_result = {"skipped": True}

    return {"open_orders": oo_result, "fills": fills_result, "cancel_sweep": sweep_result}


async def _default_async_runner(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """Fallback dispatcher: ``asyncio.to_thread``. Used only when no role-pinned
    runner is wired by the caller (existing unit tests, ad-hoc invocations)."""
    return await asyncio.to_thread(fn, *args, **kwargs)


async def activity_poller_loop(
    *,
    ib_client_factory: Callable[[], Any],
    scope: AccountScope,
    interval_s: float = DEFAULT_POLL_INTERVAL_S,
    lookback_days: int = 7,
    async_runner: Optional[Callable[..., Awaitable[Any]]] = None,
) -> None:
    """Forever-loop wrapper around run_activity_poll_tick.

    Called from the FastAPI lifespan as a background task. Survives any
    single-tick failure (logged + slept off). Exits cleanly on
    ``asyncio.CancelledError`` from the lifespan shutdown path.

    ``async_runner`` lets the caller pin the tick to a specific worker
    thread — production wires this to ``ib_pool.run_sync("sync", ...)`` so
    every tick runs on the role-pinned thread whose loop ib_async owns. If
    omitted, falls back to bare ``asyncio.to_thread`` (test/dev only — this
    is the path that was raising ``no current event loop in thread`` after
    a Gateway bounce in production).
    """
    runner = async_runner or _default_async_runner
    logger.info(
        "ib_activity_mirror: poller starting for scope=%s interval=%ss",
        scope.as_dict(),
        interval_s,
    )
    while True:
        try:
            result = await runner(
                run_activity_poll_tick,
                ib_client_factory=ib_client_factory,
                scope=scope,
                lookback_days=lookback_days,
            )
            oo = result.get("open_orders") or {}
            fills = result.get("fills") or {}
            sweep = result.get("cancel_sweep") or {}
            logger.info(
                "ib_activity_mirror tick: open_orders[reg=%s upd=%s skip=%s] "
                "fills[ins=%s upd=%s rep=%s] sweep[f=%s c=%s g=%s]",
                oo.get("registered"),
                oo.get("updated"),
                oo.get("skipped"),
                fills.get("inserted"),
                fills.get("updated"),
                fills.get("replayed"),
                sweep.get("filled"),
                sweep.get("cancelled"),
                sweep.get("graced"),
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
