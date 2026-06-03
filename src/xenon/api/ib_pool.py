"""IB connection pool with role-based persistent connections.

Maintains long-lived IBClient connections keyed by role (sync, orders, data).
Each role maps to a specific client_id as defined in IBClient.CLIENT_IDS.
asyncio.Lock per role ensures serialized access (IB socket is not concurrent-safe).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, Optional

logger = logging.getLogger("xenon.ib_pool")

from xenon.clients.ib_client import DEFAULT_GATEWAY_PORT, DEFAULT_HOST, POOL_ROLES, IBClient

# ---------------------------------------------------------------------------
# Owner-clientId registry (F5)
#
# Serializes short-lived IB connections that share a clientId slot — notably the
# naked-short audit (clientId 25) and cancel subprocess (20-49 range). Two
# concurrent connects on the same clientId would collide at the IB Gateway.
# This registry lets a caller claim a clientId slot in-process before
# attempting a connect, and blocks other callers from racing the same slot.
# ---------------------------------------------------------------------------

_busy_owners: set[int] = set()
_busy_owners_lock = threading.RLock()


class ClientIdBusy(Exception):
    """Raised when acquire_owner cannot claim the requested clientId before the deadline."""

    def __init__(self, client_id: int, message: Optional[str] = None) -> None:
        self.client_id = client_id
        super().__init__(message or f"clientId {client_id} is busy")


@contextmanager
def acquire_owner(client_id: int, timeout_ms: int = 2000) -> Iterator[None]:
    """Claim `client_id` for the duration of the `with` block.

    Polls the module-level registry every 50ms until either:
      - the slot is free (claimed, yield)
      - the deadline expires (raise ClientIdBusy)

    Thread-safe via an RLock; nested claims on *different* ids from the same
    thread are allowed. Nested claims on the *same* id will deadlock-then-raise
    because the slot is already held.
    """
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    poll_interval = 0.05  # 50ms
    while True:
        with _busy_owners_lock:
            if client_id not in _busy_owners:
                _busy_owners.add(client_id)
                break
        if time.monotonic() >= deadline:
            raise ClientIdBusy(client_id)
        time.sleep(poll_interval)

    try:
        yield
    finally:
        with _busy_owners_lock:
            _busy_owners.discard(client_id)


def _connect_in_thread(host: str, port: int, client_id: int, timeout: int = 5) -> IBClient:
    """Connect an IBClient in a thread with its own event loop.

    ib_async needs an event loop in the connecting thread. When called
    from asyncio.to_thread(), the thread has no loop by default.
    """
    import asyncio as _aio

    try:
        _aio.get_event_loop()
    except RuntimeError:
        _aio.set_event_loop(_aio.new_event_loop())

    client = IBClient()
    client.connect(host=host, port=port, client_id=client_id, timeout=timeout)
    return client


class IBPool:
    """Role-based IB connection pool.

    Usage:
        pool = IBPool()
        await pool.connect_all()

        async with pool.acquire("sync") as client:
            positions = client.get_positions()

        await pool.disconnect_all()
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_GATEWAY_PORT,
    ):
        self._host = host
        self._port = port
        self._clients: Dict[str, IBClient] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        # Sync reconnect serialization — coexists with the async _locks above.
        # Used only by ``get_with_reconnect_sync`` to coalesce racing sync
        # callers (boot rehydrate / fills replay / activity poller tick) so
        # only one reconnect attempt hits the IB clientId slot at a time.
        self._sync_reconnect_locks: Dict[str, threading.Lock] = {}
        # Per-role single-worker executors. Pinning every sync IB operation
        # for a role to ONE thread keeps the asyncio event loop ib_async sets
        # up at connect time current for every subsequent call. With the
        # default ``asyncio.to_thread`` executor (variable worker pool),
        # a later tick lands on a thread that never saw the loop and
        # ib_async raises ``no current event loop``.
        self._role_executors: Dict[str, ThreadPoolExecutor] = {}
        self._connected: Dict[str, bool] = {}

        for role in POOL_ROLES:
            self._locks[role] = asyncio.Lock()
            self._sync_reconnect_locks[role] = threading.Lock()
            self._role_executors[role] = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"ib_pool_{role}",
            )
            self._connected[role] = False

    async def connect_all(self) -> Dict[str, bool]:
        """Connect all pool roles. Returns status per role.

        Non-blocking: if IB Gateway is down, roles start disconnected.
        IB-dependent endpoints will return 503; UW-only endpoints still work.
        """
        status = {}
        loop = asyncio.get_running_loop()
        for i, (role, client_id) in enumerate(POOL_ROLES.items()):
            # IB Gateway rate-limits rapid successive connections — stagger by 1s
            if i > 0:
                await asyncio.sleep(1)

            connected = False
            for attempt in range(3):
                try:
                    # Pin connect to the role's executor so the event loop
                    # ib_async creates lands on the worker that every later
                    # run_sync call will reuse.
                    client = await loop.run_in_executor(
                        self._role_executors[role],
                        _connect_in_thread,
                        self._host,
                        self._port,
                        client_id,
                        10,
                    )
                    self._clients[role] = client
                    self._connected[role] = True
                    status[role] = True
                    connected = True
                    logger.info("IB pool: %s connected (client_id=%d)", role, client_id)
                    break
                except Exception as e:
                    if attempt < 2:
                        logger.info("IB pool: %s attempt %d failed, retrying in 2s: %s", role, attempt + 1, e)
                        await asyncio.sleep(2)
                    else:
                        self._connected[role] = False
                        status[role] = False
                        logger.warning("IB pool: %s failed to connect after 3 attempts: %s", role, e)

        return status

    async def disconnect_all(self) -> None:
        """Disconnect all pool connections + shut down role executors."""
        loop = asyncio.get_running_loop()
        for role, client in list(self._clients.items()):
            try:
                await loop.run_in_executor(
                    self._role_executors[role], client.disconnect
                )
                logger.info("IB pool: %s disconnected", role)
            except Exception as e:
                logger.warning("IB pool: %s disconnect error: %s", role, e)
            self._connected[role] = False
        self._clients.clear()
        for role, executor in self._role_executors.items():
            executor.shutdown(wait=False, cancel_futures=True)

    def get(self, role: str) -> Optional[IBClient]:
        """Get the client for a role (may be None if not connected)."""
        if role not in POOL_ROLES:
            raise ValueError(f"Unknown pool role: {role}. Valid: {list(POOL_ROLES.keys())}")
        return self._clients.get(role)

    def is_connected(self, role: str) -> bool:
        """Check if a role's connection is active."""
        client = self._clients.get(role)
        if client is None:
            return False
        try:
            return client.ib.isConnected()
        except Exception:
            return False

    def get_with_reconnect_sync(self, role: str) -> IBClient:
        """Sync sibling of ``acquire()`` for callers running in worker threads.

        Boot rehydrate, fills replay, and the activity poller dispatch work to
        ``asyncio.to_thread`` and receive their IB client via a sync factory.
        Plain ``get(role)`` returns whatever is cached — including a client
        whose socket dropped during a Gateway bounce — so the caller hits
        ``Not connected to IB`` on every tick until the FastAPI container is
        manually restarted. This method closes that gap: it checks liveness
        and, if stale, performs the same reconnect ``acquire()`` does, but
        without the async lock (we're already off-loop).

        Concurrent callers coalesce via a per-role ``threading.Lock`` so we
        never race two connects on the same IB clientId slot. Raises
        ``ConnectionError`` on reconnect failure (same shape as ``acquire()``).
        """
        if role not in POOL_ROLES:
            raise ValueError(f"Unknown pool role: {role}. Valid: {list(POOL_ROLES.keys())}")

        if self.is_connected(role):
            return self._clients[role]

        with self._sync_reconnect_locks[role]:
            if self.is_connected(role):
                return self._clients[role]

            client_id = POOL_ROLES[role]
            try:
                client = _connect_in_thread(self._host, self._port, client_id, 5)
            except Exception as exc:
                self._connected[role] = False
                logger.warning("IB pool: %s sync reconnect failed: %s", role, exc)
                raise ConnectionError(f"IB pool: {role} reconnect failed: {exc}") from exc

            self._clients[role] = client
            self._connected[role] = True
            logger.info("IB pool: %s sync-reconnected (client_id=%d)", role, client_id)
            return client

    def acquire(self, role: str) -> _PoolContext:
        """Acquire exclusive access to a role's connection.

        Usage:
            async with pool.acquire("sync") as client:
                data = client.get_positions()
        """
        return _PoolContext(self, role)

    async def _reconnect(self, role: str) -> bool:
        """Attempt to reconnect a disconnected role on its pinned worker."""
        client_id = POOL_ROLES[role]
        loop = asyncio.get_running_loop()
        try:
            client = await loop.run_in_executor(
                self._role_executors[role],
                _connect_in_thread,
                self._host,
                self._port,
                client_id,
                5,
            )
            self._clients[role] = client
            self._connected[role] = True
            logger.info("IB pool: %s reconnected (client_id=%d)", role, client_id)
            return True
        except Exception as e:
            self._connected[role] = False
            logger.warning("IB pool: %s reconnect failed: %s", role, e)
            return False

    async def run_sync(self, role: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run ``fn(*args, **kwargs)`` on the role's pinned worker thread.

        Every sync IB call for a role MUST be dispatched through this method
        rather than ``asyncio.to_thread``. The role's executor is a single
        worker, so the asyncio event loop that ``_connect_in_thread`` set up
        at connect time stays current for every subsequent call. With bare
        ``asyncio.to_thread`` the default executor's worker rotation drops
        future calls onto threads with no loop, and ib_async raises
        ``There is no current event loop in thread '...'``.

        Callable inside ``fn`` may freely use ``pool.get_with_reconnect_sync``;
        the reconnect runs inline on the same worker thread, preserving the
        loop invariant.
        """
        if role not in POOL_ROLES:
            raise ValueError(f"Unknown pool role: {role}. Valid: {list(POOL_ROLES.keys())}")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._role_executors[role],
            functools.partial(fn, *args, **kwargs),
        )

    def status(self) -> dict:
        """Return pool status for health endpoint."""
        return {
            role: {
                "connected": self.is_connected(role),
                "client_id": POOL_ROLES[role],
            }
            for role in POOL_ROLES
        }


class _PoolContext:
    """Async context manager for exclusive role access."""

    def __init__(self, pool: IBPool, role: str):
        self._pool = pool
        self._role = role

    async def __aenter__(self) -> IBClient:
        await self._pool._locks[self._role].acquire()

        # Auto-reconnect if connection dropped
        if not self._pool.is_connected(self._role):
            reconnected = await self._pool._reconnect(self._role)
            if not reconnected:
                self._pool._locks[self._role].release()
                raise ConnectionError(f"IB pool: {self._role} is not connected")

        client = self._pool.get(self._role)
        if client is None:
            self._pool._locks[self._role].release()
            raise ConnectionError(f"IB pool: {self._role} has no client")

        return client

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._pool._locks[self._role].release()
        return False
