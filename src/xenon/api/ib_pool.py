"""IB connection pool with role-based persistent connections.

Maintains long-lived IBClient connections keyed by role (sync, orders, data).
Each role maps to ``XENON_IB_CLIENT_ID_BASE`` + its ``POOL_ROLES`` offset; only
this module opens those ids (``IBClient.connect(pool_role=...)``).
asyncio.Lock per role ensures serialized access (IB socket is not concurrent-safe).

The ``orders`` role is never reconnected from a request path: only the
order owner's supervisor (``xenon.api.order_owner``) replaces it (A2).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, Optional

logger = logging.getLogger("xenon.ib_pool")

from xenon.clients.ib_client import (
    DEFAULT_GATEWAY_PORT,
    DEFAULT_HOST,
    POOL_ROLES,
    READINESS_UNKNOWN,
    IBClient,
    client_id_base,
    pool_client_id,
)

# The role that owns every order action (PR-10).
ORDERS_ROLE = "orders"

# Database names that identify the production stack. Base 0 (the prod clientId
# block) belongs to the prod DB only; every other DB must use another base.
PROD_DATABASE_NAMES = frozenset({"core_dev"})


def _database_name(database_url: Optional[str]) -> Optional[str]:
    if not database_url:
        return None
    try:
        from sqlalchemy.engine import make_url

        url = make_url(database_url)
    except Exception:  # noqa: BLE001 — unparseable: unknown, treated as non-prod
        return None
    override = url.query.get("dbname")
    if isinstance(override, (tuple, list)):
        override = override[-1] if override else None
    return override or url.database


def check_client_id_base_mapping(database_url: Optional[str], *, test_session: bool = False) -> int:
    """Refuse an unsafe ``XENON_IB_CLIENT_ID_BASE`` ↔ database pairing (A5).

    Called once at API startup, before the pool connects.

    - prod DB (``core_dev``) → base must be 0;
    - any other DB, or none → base must not be 0, so a dev API never opens
      the prod pool ids (3/4/5) on a shared gateway (``dev.sh`` exports 100).

    ``test_session`` (the pytest broker guard, ``XENON_TEST_BROKER_GUARD=1``)
    allows base 0 with a non-prod DB: the guard already refuses every gateway
    but the local paper one. This is an operational contract, not fencing —
    two environments that share a base can still collide.

    Returns the base; raises ``RuntimeError`` on a mismatch.
    """
    base = client_id_base()
    dbname = _database_name(database_url)
    if dbname in PROD_DATABASE_NAMES:
        if base != 0:
            raise RuntimeError(
                f"XENON_IB_CLIENT_ID_BASE={base} with the prod database {dbname!r}: "
                "the prod stack uses base 0 — refusing to start"
            )
        return base
    if base == 0 and not test_session:
        raise RuntimeError(
            f"XENON_IB_CLIENT_ID_BASE=0 (prod clientId block) with non-prod database {dbname!r}: "
            "set another base (dev.sh exports 100) — refusing to start"
        )
    return base


def _role_for_client_id(client_id: int) -> str:
    for role in POOL_ROLES:
        if pool_client_id(role) == client_id:
            return role
    raise ValueError(f"clientId {client_id} is not a pool role id in this environment")


def _connect_in_thread(host: str, port: int, client_id: int, timeout: int = 5) -> IBClient:
    """Connect a pool-role IBClient in a thread with its own event loop.

    ib_async needs an event loop in the connecting thread. When called
    from asyncio.to_thread(), the thread has no loop by default.

    After the socket is up the connection starts at broker readiness
    ``unknown``; one open-orders round trip (A4) runs here, on the same
    thread, so a healthy gateway reports ``ready`` at once and a gateway in
    an outage stays ``unknown``. A failed round trip never fails the connect.
    """
    import asyncio as _aio

    try:
        _aio.get_event_loop()
    except RuntimeError:
        _aio.set_event_loop(_aio.new_event_loop())

    client = IBClient()
    client.connect(host=host, port=port, client_id=client_id, timeout=timeout, pool_role=_role_for_client_id(client_id))
    client.confirm_broker_ready()
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
            self._connected[role] = False

        self._ensure_role_executors()

    def _ensure_role_executors(self) -> None:
        """Idempotently (re)create the per-role executors.

        Called from ``__init__`` for first start, and from ``connect_all`` so
        the auto-restart recovery flow (which calls
        ``disconnect_all → connect_all`` on the same pool instance) keeps
        working after ``disconnect_all`` has shut its executors down. Without
        this, ``connect_all`` would hit ``RuntimeError: cannot schedule new
        futures after shutdown`` on every role and silently report the pool
        dead.
        """
        for role in POOL_ROLES:
            existing = self._role_executors.get(role)
            if existing is None or existing._shutdown:
                self._role_executors[role] = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix=f"ib_pool_{role}",
                )

    async def connect_all(self, roles: Optional[Iterable[str]] = None) -> Dict[str, bool]:
        """Connect pool roles (default: all). Returns status per role.

        Non-blocking: if IB Gateway is down, roles start disconnected.
        IB-dependent endpoints will return 503; UW-only endpoints still work.
        """
        # Defensive: the auto-restart wrapper calls this on
        # the same pool instance after disconnect_all has shut executors down.
        self._ensure_role_executors()

        status = {}
        loop = asyncio.get_running_loop()
        for i, role in enumerate(roles or POOL_ROLES):
            client_id = pool_client_id(role)
            # IB Gateway rate-limits rapid successive connections — stagger by 1s
            if i > 0:
                await asyncio.sleep(1)

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

    async def disconnect_all(self, roles: Optional[Iterable[str]] = None) -> None:
        """Disconnect pool connections (default: all).

        With every role, the role executors shut down too (queued work is
        cancelled). With a subset — the gateway-restart path, which leaves
        ``orders`` to the order owner's supervisor — the executors stay up.
        """
        loop = asyncio.get_running_loop()
        only = set(roles) if roles is not None else None
        for role, client in list(self._clients.items()):
            if only is not None and role not in only:
                continue
            try:
                await loop.run_in_executor(
                    self._role_executors[role], client.disconnect
                )
                logger.info("IB pool: %s disconnected", role)
            except Exception as e:
                logger.warning("IB pool: %s disconnect error: %s", role, e)
            self._connected[role] = False
            self._clients.pop(role, None)
        if only is not None:
            return
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
        if role == ORDERS_ROLE:
            raise ValueError("the orders role is reconnected only by the order owner's supervisor")

        if self.is_connected(role):
            return self._clients[role]

        with self._sync_reconnect_locks[role]:
            if self.is_connected(role):
                return self._clients[role]

            client_id = pool_client_id(role)
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

    async def reconnect_role(self, role: str) -> None:
        """Replace a role's connection on its pinned worker; raise on failure.

        The old client (if any) is disconnected first on the same worker so
        its socket and readiness state never outlive the replacement.
        """
        client_id = pool_client_id(role)
        loop = asyncio.get_running_loop()
        old = self._clients.pop(role, None)
        self._connected[role] = False
        if old is not None:
            try:
                await loop.run_in_executor(self._role_executors[role], old.disconnect)
            except Exception as exc:  # noqa: BLE001 — a dead socket may refuse a clean close
                logger.debug("IB pool: %s old client disconnect failed: %s", role, exc)
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

    async def _reconnect(self, role: str) -> bool:
        """Attempt to reconnect a disconnected role on its pinned worker."""
        try:
            await self.reconnect_role(role)
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

    def broker_readiness(self, role: str) -> str:
        """Broker readiness of a role's connection (A4), separate from the socket.

        ``unknown`` whenever the socket is down or no client exists — a socket
        replacement always restarts at ``unknown``.
        """
        if not self.is_connected(role):
            return READINESS_UNKNOWN
        state = getattr(self._clients.get(role), "broker_readiness", READINESS_UNKNOWN)
        return state if isinstance(state, str) else READINESS_UNKNOWN

    def status(self) -> dict:
        """Return pool status for health endpoint."""
        return {
            role: {
                "connected": self.is_connected(role),
                "broker_readiness": self.broker_readiness(role),
                "client_id": pool_client_id(role),
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

        # Auto-reconnect if connection dropped — never for ``orders``: only
        # the order owner's supervisor replaces that connection (A2).
        if not self._pool.is_connected(self._role):
            if self._role == ORDERS_ROLE:
                self._pool._locks[self._role].release()
                raise ConnectionError(f"IB pool: {self._role} is not connected")
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
