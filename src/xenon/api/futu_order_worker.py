"""Futu order worker (X3c contract item 6).

One worker thread owns one Futu trade context, separate from the
observation pass's context and from the IB pool, so a hung Futu call never
blocks the IB order worker. The worker is generic: it runs a ``core(ctx,
mark_sent) -> dict`` callable and never imports ``xenon.execution.futu_orders``
(the caller guard allows only ``services/order_entry.py`` to do that; the core
closure there calls the adapter with this worker's ctx and ``mark_sent``).

Invocation / settlement semantics are the IB owner's (``order_owner``):
queued work that is cancelled or times out before it starts never runs;
started work stays owned and settles on the worker exactly once, also after
the caller timed out; with the possible-send mark set, an exception or a
non-ok reply is ``unconfirmed``, never not sent.

Before each core the worker does its own readiness check: a fresh
``get_acc_list()`` round trip (trade/open_trade_context.py:65) and
``futu_scope.verify_live`` for the configured scope. Not ready → refused,
nothing sent. The context opens lazily on the worker thread, and a failed
check or a core exception closes it (the next job reopens it).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from typing import Any, Callable, Optional

from xenon.api.order_owner import (
    BROKER_DISCONNECTED,
    Invocation,
    _not_sent,
    _settle,
    _unconfirmed,
    await_invocation,
    finish_job,
)
from xenon.execution import broker_capabilities, futu_scope
from xenon.execution.account_scope import AccountScope

logger = logging.getLogger("xenon.futu_order_worker")

RET_OK = 0  # futu/common/constant.py:184

WORKER_STOPPED = "FUTU_WORKER_STOPPED"
WORKER_BUSY = "FUTU_WORKER_BUSY"
OPEND_UNREACHABLE = "FUTU_OPEND_UNREACHABLE"
NOT_READY = "FUTU_NOT_READY"

# Live jobs (queued + running) before admission refuses. A hung call holds one
# slot; work queued behind it times out unsent.
MAX_LIVE = 8

Core = Callable[[Any, Callable[[], None]], dict]
Connector = Callable[[], tuple[Any, Callable[[], None]]]


def _refused(message: str, reason_code: str) -> dict:
    return {**_not_sent(f"{message}; nothing was sent", BROKER_DISCONNECTED), "reason_code": reason_code}


class FutuOrderWorker:
    def __init__(self, scope: AccountScope, *, connect: Connector, max_live: int = MAX_LIVE) -> None:
        self.scope = scope
        self._connect = connect
        self._max_live = max_live
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="xenon-futu-orders")
        # Worker-thread state: only `_execute` / `_retire_ctx` touch it.
        self._ctx: Any = None
        self._close: Optional[Callable[[], None]] = None
        self._live: set[Invocation] = set()
        self._stopping = False

    def admission(self) -> Optional[dict]:
        if self._stopping:
            return _refused("Futu order worker is stopped", WORKER_STOPPED)
        if len(self._live) >= self._max_live:
            return _refused("Futu order worker is busy", WORKER_BUSY)
        return None

    async def run(self, core: Core, *, timeout: float, settle: Optional[Callable[[dict], None]] = None) -> dict:
        """Run ``core(ctx, mark_sent)`` on the worker; same contract as ``OrderOwner.run``."""
        refusal = self.admission()
        if refusal is not None:
            _settle(settle, refusal)
            return refusal
        inv = Invocation()
        try:
            fut = asyncio.wrap_future(self._executor.submit(self._job, inv, core, settle))
        except RuntimeError:  # executor shut down
            refusal = _refused("Futu order worker is stopped", WORKER_STOPPED)
            _settle(settle, refusal)
            return refusal
        self._live.add(inv)
        fut.add_done_callback(lambda _f: self._live.discard(inv))
        return await await_invocation(inv, fut, timeout=timeout, settle=settle)

    # -- worker thread -------------------------------------------------------

    def _job(self, inv: Invocation, core: Core, settle) -> Optional[dict]:
        if not inv.start():
            return None  # abandoned: the request side settles
        try:
            reply = self._execute(inv, core)
        except BaseException:  # pragma: no cover — _execute never raises
            reply = _unconfirmed("Futu order action crashed") if inv.sent else _not_sent("Futu order action crashed")
        return finish_job(inv, reply, settle)

    def _execute(self, inv: Invocation, core: Core) -> dict:
        if self._stopping:
            return _refused("Futu order worker is stopped", WORKER_STOPPED)
        if self._ctx is None:
            try:
                self._ctx, self._close = self._connect()
            except Exception as exc:  # noqa: BLE001
                logger.warning("futu orders: OpenD connect failed (%s)", type(exc).__name__)
                return _refused("Futu OpenD is unreachable", OPEND_UNREACHABLE)
        try:
            ret, rows = self._ctx.get_acc_list()
        except Exception as exc:  # noqa: BLE001
            ret, rows = None, type(exc).__name__
        if ret != RET_OK:
            logger.warning("futu orders: readiness check failed (%s)", rows if ret is None else "get_acc_list not OK")
            self._retire_ctx()
            return _refused("Futu account check did not answer", NOT_READY)
        ok, reason = futu_scope.verify_live(self.scope, rows)
        if not ok:
            return _refused(f"Futu account check failed ({reason})", reason or NOT_READY)
        try:
            return core(self._ctx, inv.mark_sent)
        except Exception as exc:  # noqa: BLE001 — after the mark this is unconfirmed, never unsent
            # Type only: broker text is not logged here (the adapter redacts its own).
            logger.warning("futu order action failed (sent=%s, %s)", inv.sent, type(exc).__name__)
            self._retire_ctx()
            message = f"Futu order action failed ({type(exc).__name__})"
            return _unconfirmed(message) if inv.sent else _not_sent(message)

    def _retire_ctx(self) -> None:
        close, self._ctx, self._close = self._close, None, None
        if close is not None:
            try:
                close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("futu orders: context close failed (%s)", type(exc).__name__)

    # -- shutdown ------------------------------------------------------------

    async def stop(self, wait_s: float = 0.5) -> None:
        """Refuse new work, close the context after the running job; waits at
        most ``wait_s``. Queued jobs start after stop and are refused before
        their core (zero sends); a running job stays owned and settles when it
        ends."""
        self._stopping = True
        try:
            cleanup = self._executor.submit(self._retire_ctx)
        except RuntimeError:  # already shut down
            cleanup = None
        self._executor.shutdown(wait=False)
        if cleanup is not None:
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(cleanup)), wait_s)
            except TimeoutError:
                logger.warning("futu orders: a job is still running at stop; the context closes after it")


def start_futu_order_worker(
    *, connector: Optional[Callable[[AccountScope], Connector]] = None
) -> Optional[FutuOrderWorker]:
    """The worker for the configured Futu scope, or None under
    ``XENON_READ_ONLY=1``, without a valid scope, or with the capability off
    (``XENON_FUTU_TRADING`` not 1). Opens nothing until the first job."""
    if os.environ.get("XENON_READ_ONLY") == "1":
        logger.info("futu order worker skipped: XENON_READ_ONLY=1")
        return None
    ok, _, cause = broker_capabilities.evaluate("FUTU", "place_single")
    if not ok:
        logger.info("futu order worker skipped: %s", cause)
        return None
    scope, _ = futu_scope.configured_scope()
    if connector is None:
        from xenon.api.services.futu_observation import futu_client_connector as connector
    logger.info("futu order worker ready (%s)", scope.account_env)
    return FutuOrderWorker(scope, connect=connector(scope))
