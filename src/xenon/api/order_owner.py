"""Single order owner (PR-10): every order action runs on the pool ``orders``
connection (``XENON_IB_CLIENT_ID_BASE`` + 4), on its pinned single-worker
executor. Nothing here spawns a subprocess or opens a second IB connection.

Three parts:

- ``Invocation`` — the invocation-local record ``queued → started → sent →
  done`` (or ``abandoned``). Only a job abandoned before it started, or one
  refused before the possible-send mark, is proven unsent.
- ``OrderOwner.run`` — the request side: admission check, queue the job,
  wait up to the deadline. Timed out while queued → abandoned (zero sends);
  timed out after start → ``unconfirmed`` and the WORKER settles (the
  ``settle`` callback runs on the worker for every started job, exactly once).
- ``OrderOwner.supervise`` — the recovery owner for ``orders`` (A2). A socket
  loss that is positively a gateway outage (1100/2110 seen before the loss,
  gateway port not listening, the whole pool lost, or a noted gateway
  restart) → reconnect with backoff on the orders executor. Anything else →
  latched ``ORDERS_HALTED``: zero reconnect attempts, CRITICAL log, red
  ``/health``, until an operator restarts the API.

Right before any send (place, modify, cancel, wizard protect) the worker
re-checks the halt latch and does a fresh broker round trip
(``IBClient.confirm_broker_ready``); not READY → refused, zero sends (A4).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Optional

from xenon.api.ib_pool import ORDERS_ROLE
from xenon.clients.ib_client import READINESS_LOST, READINESS_READY, ClientIdInUse, pool_client_id

logger = logging.getLogger("xenon.order_owner")

# Reply classifications the owner adds (mapped to HTTP by the routes).
ORDERS_HALTED = "orders_halted"
BROKER_DISCONNECTED = "broker_disconnected"

# Supervisor cadence and reconnect backoff (seconds).
POLL_S = 2.0
BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)


class Invocation:
    """Invocation-local lifecycle record; transitions are atomic."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = "queued"
        self.sent = False  # the possible-send mark; never cleared

    def _move(self, allowed: tuple[str, ...], to: str) -> bool:
        with self._lock:
            if self.state not in allowed:
                return False
            self.state = to
            return True

    def start(self) -> bool:
        return self._move(("queued",), "started")

    def abandon(self) -> bool:
        """True when the job never started (and now never will)."""
        return self._move(("queued", "abandoned"), "abandoned")

    def mark_sent(self) -> None:
        if self._move(("started", "sent"), "sent"):
            self.sent = True

    def finish(self) -> None:
        self._move(("started", "sent"), "done")


class OwnerRefused(Exception):
    """Refusal raised right before a send: nothing was transmitted."""

    def __init__(self, classification: str, message: str) -> None:
        super().__init__(message)
        self.reply = {
            "status": "error",
            "message": message,
            "classification": classification,
            "amend_outcome": "not_sent",
        }


class _GuardedClient:
    """The orders client with the pre-send check in front of every send."""

    def __init__(self, client: Any, before_send: Callable[[], None]) -> None:
        self._client = client
        self._before_send = before_send

    def place_order(self, *args, **kwargs):
        self._before_send()
        return self._client.place_order(*args, **kwargs)

    def cancel_order(self, *args, **kwargs):
        self._before_send()
        return self._client.cancel_order(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def _not_sent(message: str, classification: str = "connection") -> dict:
    return {"status": "error", "message": message, "classification": classification, "amend_outcome": "not_sent"}


def _unconfirmed(message: str) -> dict:
    return {"status": "error", "message": message, "classification": "connection", "amend_outcome": "unconfirmed"}


def _gateway_port_listening(host: str, port: int) -> bool:
    from xenon.api.ib_gateway import _port_listening

    return _port_listening(host, port)


class OrderOwner:
    def __init__(
        self,
        pool: Any,
        *,
        port_listening: Optional[Callable[[], bool]] = None,
        poll_s: float = POLL_S,
        backoff_s: tuple[float, ...] = BACKOFF_S,
    ) -> None:
        self._pool = pool
        self._port_listening = port_listening or (
            lambda: _gateway_port_listening(getattr(pool, "_host", "127.0.0.1"), getattr(pool, "_port", 0))
        )
        self._poll_s = poll_s
        self._backoff_s = backoff_s
        self._halt: Optional[str] = None
        self._stopping = False
        self._recovering = False
        self._gateway_restart_noted = False
        self._live: set[Invocation] = set()
        self._task: Optional[asyncio.Task] = None

    # -- state ---------------------------------------------------------------

    @property
    def halted(self) -> bool:
        return self._halt is not None

    def status(self) -> dict:
        return {
            "client_id": pool_client_id(ORDERS_ROLE),
            "halted": self.halted,
            "halt_reason": self._halt,
            "recovering": self._recovering,
        }

    def note_gateway_restart(self) -> None:
        """The API restarted the gateway: the coming ``orders`` loss is an outage."""
        self._gateway_restart_noted = True

    def _latch_halt(self, reason: str) -> None:
        if self._halt is None:
            self._halt = reason
            logger.critical(
                "ORDERS_HALTED: %s — no automatic reconnect of clientId %d. Confirm no other "
                "process owns that id, then restart the API.",
                reason,
                pool_client_id(ORDERS_ROLE),
            )

    def admission(self) -> Optional[dict]:
        """Refusal reply when no order action may be queued now, else None."""
        if self._halt is not None:
            return _not_sent(f"Order actions halted: {self._halt}. Restart the API.", ORDERS_HALTED)
        if self._stopping or not self._pool.is_connected(ORDERS_ROLE):
            return _not_sent("IB orders connection is down; nothing was sent", BROKER_DISCONNECTED)
        if self._pool.broker_readiness(ORDERS_ROLE) == READINESS_LOST:
            return _not_sent("IB reports broker connectivity lost; nothing was sent", BROKER_DISCONNECTED)
        return None

    # -- request side --------------------------------------------------------

    async def run(
        self,
        core: Callable[[Any], dict],
        *,
        timeout: float,
        settle: Optional[Callable[[dict], None]] = None,
    ) -> dict:
        """Run ``core(client)`` on the orders worker; return its reply dict.

        ``settle(reply)`` runs exactly once: on the worker after a started
        job (even when this request is gone), or here when the job provably
        never started.
        """
        refusal = self.admission()
        if refusal is not None:
            _settle(settle, refusal)
            return refusal
        inv = Invocation()
        self._live.add(inv)
        fut = asyncio.ensure_future(self._pool.run_sync(ORDERS_ROLE, self._job, inv, core, settle))
        fut.add_done_callback(lambda _f: self._live.discard(inv))
        try:
            done, _ = await asyncio.wait({fut}, timeout=timeout)
        except asyncio.CancelledError:
            if inv.abandon():
                _settle(settle, _not_sent("Request cancelled before the order action started; nothing was sent"))
            raise
        if fut in done and not fut.cancelled() and fut.exception() is None and fut.result() is not None:
            return fut.result()
        if inv.abandon():
            reply = _not_sent("Order action did not start before the deadline; nothing was sent")
            _settle(settle, reply)
            return reply
        return _unconfirmed(
            "Order action started but its outcome is not confirmed yet; check working orders. Do not resubmit."
        )

    # -- worker side (orders executor thread) ----------------------------------

    def _job(self, inv: Invocation, core: Callable[[Any], dict], settle) -> Optional[dict]:
        if not inv.start():
            return None  # abandoned: the request side settles
        try:
            reply = self._execute(inv, core)
        except BaseException:  # pragma: no cover — _execute never raises
            reply = _unconfirmed("Order action crashed") if inv.sent else _not_sent("Order action crashed")
        if inv.sent and reply.get("status") != "ok" and reply.get("amend_outcome") in (None, "not_sent"):
            # Possibly transmitted and nothing proves it was not applied.
            if reply.get("classification") != "ib_reject":
                reply = {**reply, "amend_outcome": "unconfirmed"}
        inv.finish()
        _settle(settle, reply)
        return reply

    def _execute(self, inv: Invocation, core: Callable[[Any], dict]) -> dict:
        if self._halt is not None:
            return OwnerRefused(ORDERS_HALTED, f"Order actions halted: {self._halt}").reply
        client = self._pool.get(ORDERS_ROLE)
        if client is None or not self._pool.is_connected(ORDERS_ROLE):
            return _not_sent("IB orders connection is down; nothing was sent", BROKER_DISCONNECTED)

        def before_send() -> None:
            if self._halt is not None:
                raise OwnerRefused(ORDERS_HALTED, f"Order actions halted: {self._halt}; nothing was sent")
            if client.confirm_broker_ready() != READINESS_READY:
                raise OwnerRefused(BROKER_DISCONNECTED, "IB broker connectivity not confirmed; nothing was sent")
            inv.mark_sent()

        try:
            return core(_GuardedClient(client, before_send))
        except OwnerRefused as refused:
            return refused.reply
        except Exception as exc:  # noqa: BLE001 — after the send mark this is unconfirmed, never unsent
            logger.exception("order action failed (sent=%s)", inv.sent)
            message = f"Order action failed: {exc}"
            return _unconfirmed(message) if inv.sent else _not_sent(message)

    # -- supervisor ----------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self.supervise(), name="orders-owner-supervisor")

    async def stop(self) -> None:
        """Intentional shutdown: no reconnect, queued jobs abandoned (zero sends)."""
        self._stopping = True
        for inv in list(self._live):
            inv.abandon()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _probe(self) -> tuple[bool, bool]:
        """(socket up, broker loss seen) — runs on the orders worker.

        ib_async processes socket events (1100, connection closed) only while
        its loop runs on this thread, so pump it once before reading.
        """
        client = self._pool.get(ORDERS_ROLE)
        if client is None:
            return False, False
        lost = getattr(client, "broker_readiness", None) == READINESS_LOST
        try:
            client.ib.sleep(0)
        except Exception:  # noqa: BLE001 — a dead socket may raise here
            pass
        lost = lost or getattr(client, "broker_readiness", None) == READINESS_LOST
        return self._pool.is_connected(ORDERS_ROLE), lost

    async def supervise(self) -> None:
        """First check at once (startup during an outage), then every poll."""
        first = True
        while not self._stopping and self._halt is None:
            if not first:
                await asyncio.sleep(self._poll_s)
            first = False
            if self._stopping:
                return
            try:
                connected, lost = await self._pool.run_sync(ORDERS_ROLE, self._probe)
            except RuntimeError:  # executor shut down: shutdown in progress
                return
            if not connected and not self._stopping:
                await self._on_loss(broker_loss_seen=lost)

    def _whole_pool_lost(self) -> bool:
        return not any(self._pool.is_connected(r) for r in ("sync", "data"))

    async def _on_loss(self, *, broker_loss_seen: bool) -> None:
        restart_noted, self._gateway_restart_noted = self._gateway_restart_noted, False
        outage = (
            broker_loss_seen
            or restart_noted
            or self._whole_pool_lost()
            or not await asyncio.to_thread(self._port_listening)
        )
        if not outage:
            self._latch_halt("orders connection lost while the gateway and the other pool roles were up")
            return
        logger.warning("IB orders connection lost in a gateway outage — supervised reconnect")
        await self._recover()

    async def _recover(self) -> None:
        self._recovering = True
        try:
            attempt = 0
            while not self._stopping and self._halt is None:
                try:
                    await self._pool.reconnect_role(ORDERS_ROLE)
                    return
                except ClientIdInUse as exc:
                    self._latch_halt(f"clientId {pool_client_id(ORDERS_ROLE)} is in use by another process ({exc})")
                    return
                except Exception as exc:  # noqa: BLE001 — gateway still down: back off
                    delay = self._backoff_s[min(attempt, len(self._backoff_s) - 1)]
                    attempt += 1
                    logger.info("orders reconnect attempt %d failed (%s); retry in %.0fs", attempt, exc, delay)
                    await asyncio.sleep(delay)
        finally:
            self._recovering = False


def _settle(settle: Optional[Callable[[dict], None]], reply: dict) -> None:
    if settle is None:
        return
    try:
        settle(reply)
    except Exception:  # noqa: BLE001 — Gate 4 expiry resolves an unsettled amend
        logger.exception("order action settlement failed")
