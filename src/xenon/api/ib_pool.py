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
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, Optional

logger = logging.getLogger("xenon.ib_pool")

from xenon.clients.ib_client import (
    DEFAULT_GATEWAY_PORT,
    DEFAULT_HOST,
    POOL_ROLES,
    READINESS_LOST,
    READINESS_READY,
    READINESS_UNKNOWN,
    IBClient,
    client_id_base,
    pool_client_id,
)

# The role that owns every order action (PR-10).
ORDERS_ROLE = "orders"

# The quote / qualify role. A job on it that its caller abandoned while it
# runs fences the role until the job returns (see ``IBPool.run_sync``).
DATA_ROLE = "data"
_FENCED_ROLES = frozenset({DATA_ROLE})

# Deadline for one request-path quote / qualify call on the data role
# (lock wait + reconnect + IB work), incident 2026-10-26.
DEFAULT_IB_QUOTE_TIMEOUT_S = 10.0
# get_quote(snapshot=True) waits this long after reqMktData; the in-thread
# request bound leaves room for it inside the deadline.
_SNAPSHOT_WAIT_S = 2.0
_MIN_REQUEST_TIMEOUT_S = 0.5


class IBDataTimeout(ConnectionError):
    """A data-role call did not finish within ``XENON_IB_QUOTE_TIMEOUT_S``."""


class IBUpstreamLost(ConnectionError):
    """The gateway reported 1100 / 2110: socket open, IB servers unreachable."""


class IBRoleFenced(ConnectionError):
    """An abandoned job still runs on the role's worker; refused at once."""


def ib_quote_timeout_s() -> float:
    """``XENON_IB_QUOTE_TIMEOUT_S`` (default 10); invalid / non-positive → default."""
    raw = os.environ.get("XENON_IB_QUOTE_TIMEOUT_S", "")
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_IB_QUOTE_TIMEOUT_S
    return value if value > 0 else DEFAULT_IB_QUOTE_TIMEOUT_S


def data_deadline() -> float:
    """Monotonic deadline ``XENON_IB_QUOTE_TIMEOUT_S`` from now (for ``bounded_data_job``)."""
    return time.monotonic() + ib_quote_timeout_s()


def bounded_data_job(deadline_at: float, fn: Callable[..., Any], client: Any, *args: Any) -> Any:
    """Worker side of ``call_data_bounded``: refuse on a lost upstream, then
    run ``fn`` with every blocking ib_async request bounded by the time left.

    ``IB.RequestTimeout`` bounds each sync wrapper (qualifyContracts,
    reqContractDetails, ...) so the worker thread frees itself even when the
    gateway never answers. It is set only for this job and restored after:
    the role's single worker never runs two jobs on the client at once.
    """
    pump = getattr(client, "pump_events", None)
    if callable(pump):
        pump()
    if getattr(client, "broker_readiness", None) == READINESS_LOST:
        code = getattr(client, "readiness_code", None)
        raise IBUpstreamLost(f"IB Gateway is not connected to IB servers (code {code})")
    remaining = deadline_at - time.monotonic()
    if remaining <= 0:
        raise IBDataTimeout("IB data call deadline passed before it started")
    ib = getattr(client, "ib", None)
    previous = getattr(ib, "RequestTimeout", None)
    if ib is not None:
        ib.RequestTimeout = max(_MIN_REQUEST_TIMEOUT_S, remaining - _SNAPSHOT_WAIT_S)
    try:
        return fn(client, *args)
    except asyncio.TimeoutError as exc:
        raise IBDataTimeout(f"IB did not answer within {remaining:.1f}s") from exc
    finally:
        if ib is not None:
            ib.RequestTimeout = previous


async def call_data_bounded(pool: Any, fn: Callable[..., Any], *args: Any, timeout: Optional[float] = None) -> Any:
    """Run ``fn(client, *args)`` on the data role within one deadline.

    The deadline covers the lock wait, any reconnect and the IB work. Raises
    ``IBDataTimeout`` past it, ``IBUpstreamLost`` at once when the data
    connection reports 1100 / 2110, ``IBRoleFenced`` while an abandoned job
    still runs, ``ConnectionError`` when the role is down. Never leaves the
    role lock held; an abandoned running job fences the role (``run_sync``).
    """
    timeout = ib_quote_timeout_s() if timeout is None else timeout
    deadline_at = time.monotonic() + timeout
    try:
        async with asyncio.timeout(timeout):
            async with pool.acquire(DATA_ROLE) as client:
                return await pool.run_sync(DATA_ROLE, bounded_data_job, deadline_at, fn, client, *args)
    except TimeoutError as exc:
        raise IBDataTimeout(f"IB data call did not finish within {timeout:g}s") from exc


@asynccontextmanager
async def data_session(pool: Any, prep: Callable[..., Any], *args: Any, timeout: Optional[float] = None):
    """Hold the data role for a longer job whose first step is bounded.

    Lock wait + reconnect + ``prep(client, *args)`` (e.g. qualify) run under
    one ``XENON_IB_QUOTE_TIMEOUT_S`` deadline with the same worker bound and
    fencing as ``call_data_bounded``; then the client is yielded with the
    lock still held for the caller's own (separately timed) work.
    """
    timeout = ib_quote_timeout_s() if timeout is None else timeout
    deadline_at = time.monotonic() + timeout
    ctx = pool.acquire(DATA_ROLE)
    try:
        async with asyncio.timeout(timeout):
            client = await ctx.__aenter__()
    except TimeoutError as exc:
        raise IBDataTimeout(f"IB data role not acquired within {timeout:g}s") from exc
    try:
        try:
            async with asyncio.timeout(max(0.0, deadline_at - time.monotonic())):
                await pool.run_sync(DATA_ROLE, bounded_data_job, deadline_at, prep, client, *args)
        except TimeoutError as exc:
            raise IBDataTimeout(f"IB data call did not finish within {timeout:g}s") from exc
        yield client
    finally:
        await ctx.__aexit__(None, None, None)

# Database names that identify the production stack. Base 0 (the prod clientId
# block) belongs to the prod DB only; every other DB must use another base.
PROD_DATABASE_NAMES = frozenset({"core_dev"})


# Query keys that cannot re-route the connection. Anything else (asyncpg
# `database`, libpq `dbname`, `host`, `service`, `dsn`, ...) can point the
# engine at another database than the URL path shows, so the base check
# refuses it: allowlist, not blocklist (same keys as the pytest DB guard in
# xenon._test_db).
_SAFE_QUERY_KEYS = frozenset({"sslmode", "sslrootcert", "sslcert", "sslkey", "connect_timeout", "application_name"})


def _database_name(database_url: Optional[str]) -> Optional[str]:
    """The database the engine will connect to; None when no URL is set.

    Raises ``ValueError`` when that is not certain: unparseable URL, no
    database in the path, or a query key that can override the routing.
    """
    if not database_url:
        return None
    from sqlalchemy.engine import make_url

    try:
        url = make_url(database_url)
    except Exception as exc:  # noqa: BLE001 — sqlalchemy raises ArgumentError / ValueError
        raise ValueError(f"DATABASE_URL is not a parseable database URL ({type(exc).__name__})") from exc
    routing = sorted(k for k in url.query if k not in _SAFE_QUERY_KEYS)
    if routing:
        raise ValueError(f"DATABASE_URL query params {routing} can re-route the connection to another database")
    if not url.database:
        raise ValueError("DATABASE_URL names no database")
    return url.database


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
    try:
        dbname = _database_name(database_url)
    except ValueError as exc:
        raise RuntimeError(
            f"{exc}: cannot map XENON_IB_CLIENT_ID_BASE={base} to a database — refusing to start"
        ) from exc
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
        # Last startup connect failure per role (e.g. ClientIdInUse / 326),
        # so the orders supervisor can latch its halt with the real cause.
        self.connect_errors: Dict[str, BaseException] = {}
        # Shutdown: a full disconnect_all sets this (under the lock) before it
        # takes its client snapshot, and it is never cleared: a full close is
        # terminal (one pool per api process; the gateway-restart path uses
        # role subsets). A connect that finishes later closes its own new
        # socket instead of registering it (nobody would disconnect it).
        # ponytail: no reopen after a full close; add pool generations if a
        # caller ever needs one (Astra T5 R2-1/R3-1/R4-1 races).
        self._closing = False
        self._close_lock = threading.Lock()
        # Abandoned-but-running job per fenced role (incident 2026-10-26).
        self._fences: Dict[str, Future] = {}

        for role in POOL_ROLES:
            self._locks[role] = asyncio.Lock()
            self._sync_reconnect_locks[role] = threading.Lock()
            self._connected[role] = False
            self._role_executors[role] = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"ib_pool_{role}")

    async def connect_all(self, roles: Optional[Iterable[str]] = None) -> Dict[str, bool]:
        """Connect pool roles (default: all). Returns status per role.

        Non-blocking: if IB Gateway is down, roles start disconnected.
        IB-dependent endpoints will return 503; UW-only endpoints still work.
        After a full ``disconnect_all`` the pool is closed for good: every
        role reports False and nothing connects.
        """
        roles = list(roles) if roles is not None else list(POOL_ROLES)
        status = {role: False for role in roles}
        loop = asyncio.get_running_loop()
        for i, role in enumerate(roles):
            client_id = pool_client_id(role)
            # IB Gateway rate-limits rapid successive connections — stagger by 1s
            if i > 0:
                await asyncio.sleep(1)
            if self._closing:
                logger.info("IB pool: closed; %s not connected", role)
                break

            for attempt in range(3):
                try:
                    # Pin connect to the role's executor so the event loop
                    # ib_async creates lands on the worker that every later
                    # run_sync call will reuse.
                    await loop.run_in_executor(self._role_executors[role], self._connect_owned, role, client_id, 10)
                    status[role] = True
                    self.connect_errors.pop(role, None)
                    logger.info("IB pool: %s connected (client_id=%d)", role, client_id)
                    break
                except Exception as e:
                    if attempt < 2 and not self._closing:
                        logger.info("IB pool: %s attempt %d failed, retrying in 2s: %s", role, attempt + 1, e)
                        await asyncio.sleep(2)
                    else:
                        self._connected[role] = False
                        self.connect_errors[role] = e
                        logger.warning("IB pool: %s failed to connect (attempt %d): %s", role, attempt + 1, e)
                        break

        return status

    def _connect_owned(self, role: str, client_id: int, timeout: int) -> IBClient:
        """Worker side: connect, then register the client — or, when shutdown
        began while the connect ran, disconnect it at once. Registration runs
        on the worker, so a cancelled awaiter (owner supervisor stopped
        mid-reconnect) never orphans the socket. Never another clientId."""
        client = _connect_in_thread(self._host, self._port, client_id, timeout)
        with self._close_lock:
            if not self._closing:
                self._clients[role] = client
                self._connected[role] = True
                return client
        try:
            client.disconnect()
        except Exception as exc:  # noqa: BLE001
            logger.warning("IB pool: %s late connect close failed: %s", role, exc)
        raise ConnectionError(f"IB pool: {role} connected during shutdown; closed it")

    async def disconnect_all(self, roles: Optional[Iterable[str]] = None, timeout: Optional[float] = None) -> None:
        """Disconnect pool connections (default: all). Never raises.

        Each disconnect is queued on the role's own worker; roles run in
        parallel. ``timeout`` bounds only how long this call WAITS: a
        disconnect still queued behind running work (a started order core can
        block on a broker call with no timeout of its own) is never cancelled.
        It stays queued and the client stays registered until it has run; the
        executors shut down without dropping queued work, and interpreter exit
        joins the worker threads. If that work outlives the container's stop
        budget, SIGKILL is the backstop and the socket may stay open on the
        gateway (stale clientId — operator check: ``lsof`` the gateway port).

        A full disconnect closes the pool for good: it marks the pool closing
        (a running connect_all stops; an in-flight worker connect closes
        itself in ``_connect_owned``), then shuts the executors down. With a
        subset — the gateway-restart path, which leaves ``orders`` to the
        order owner — the executors stay up and the pool stays open.
        """
        if roles is not None:
            only = set(roles)
            await self._disconnect_targets([(r, c) for r, c in list(self._clients.items()) if r in only], timeout)
            return
        with self._close_lock:
            self._closing = True
            targets = list(self._clients.items())
        await self._disconnect_targets(targets, timeout)
        for executor in self._role_executors.values():
            executor.shutdown(wait=False)  # queued disconnects still run

    async def _disconnect_targets(self, targets: list, timeout: Optional[float]) -> None:

        async def _one(role: str, client: IBClient) -> None:
            queued = self._role_executors[role].submit(client.disconnect)
            done, _ = await asyncio.wait({asyncio.wrap_future(queued)}, timeout=timeout)
            if not done:
                logger.critical(
                    "IB pool: %s disconnect still queued after %ss behind running work; it runs "
                    "when that work ends — if the process is killed first, its clientId may stay in "
                    "use on the gateway (check the gateway for stale sockets before restart)",
                    role,
                    timeout,
                )
                return
            exc = queued.exception()
            if exc is not None:
                logger.warning("IB pool: %s disconnect error: %s", role, exc)
            else:
                logger.info("IB pool: %s disconnected", role)
            self._connected[role] = False
            if self._clients.get(role) is client:
                self._clients.pop(role, None)

        await asyncio.gather(*(_one(role, client) for role, client in targets))

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

        if self._closing:
            raise ConnectionError(f"IB pool: {role} not reconnected, pool is closing")
        with self._sync_reconnect_locks[role]:
            if self.is_connected(role):
                return self._clients[role]

            client_id = pool_client_id(role)
            try:
                client = self._connect_owned(role, client_id, 5)
            except Exception as exc:
                self._connected[role] = False
                logger.warning("IB pool: %s sync reconnect failed: %s", role, exc)
                raise ConnectionError(f"IB pool: {role} reconnect failed: {exc}") from exc

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
        old = self._clients.pop(role, None)
        self._connected[role] = False
        # Both steps go through the fence-aware dispatch: a caller deadline
        # during a running disconnect / connect fences the role until that
        # step returns, so a second caller cannot queue another connect on
        # the same clientId (Astra PR #254 finding 1).
        if old is not None:
            try:
                await self._dispatch(role, old.disconnect, fence=True)
            except Exception as exc:  # noqa: BLE001 — a dead socket may refuse a clean close
                logger.debug("IB pool: %s old client disconnect failed: %s", role, exc)
        await self._dispatch(role, functools.partial(self._connect_owned, role, client_id, 5), fence=True)
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
        return await self._dispatch(role, functools.partial(fn, *args, **kwargs), fence=role in _FENCED_ROLES)

    async def _dispatch(self, role: str, call: Callable[[], Any], *, fence: bool) -> Any:
        """Run ``call`` on the role's worker. A queued job whose caller gives
        up never starts. With ``fence``, a job already running when its caller
        gives up keeps the client until it returns: the role is fenced so
        later ``acquire`` callers fail fast instead of queueing behind it."""
        job = self._role_executors[role].submit(call)
        try:
            return await asyncio.wrap_future(job)
        except asyncio.CancelledError:
            if not job.cancel() and fence and not job.done():
                self._fence(role, job)
            raise

    def _fence(self, role: str, job: Future) -> None:
        self._fences[role] = job
        logger.warning("IB pool: %s fenced — an abandoned job still runs on its worker", role)

        def _clear(done: Future) -> None:
            if self._fences.get(role) is done:
                self._fences.pop(role, None)
                logger.info("IB pool: %s fence cleared — the abandoned job returned", role)

        job.add_done_callback(_clear)

    def is_fenced(self, role: str) -> bool:
        """True while an abandoned job still runs on the role's worker."""
        job = self._fences.get(role)
        return job is not None and not job.done()

    def upstream_status(self) -> dict:
        """Gateway ↔ IB servers state seen by the pool connections.

        ``lost`` when any connected role last saw 1100 / 2110, ``connected``
        when a role is ready (round trip or 1102) and none is lost, else
        ``unknown``. A role only sees an event once its loop runs again, so
        the value can lag (module notes in ``ib_client``).
        """
        clients = [c for r, c in list(self._clients.items()) if self.is_connected(r)]
        by_state: Dict[str, list] = {}
        for c in clients:
            by_state.setdefault(getattr(c, "broker_readiness", READINESS_UNKNOWN), []).append(c)
        if by_state.get(READINESS_LOST):
            state, group = "lost", by_state[READINESS_LOST]
        elif by_state.get(READINESS_READY):
            state, group = "connected", by_state[READINESS_READY]
        else:
            state, group = "unknown", clients
        sinces = [s for s in (getattr(c, "readiness_since", None) for c in group) if isinstance(s, (int, float))]
        # last_code = the latest event across the group (event time, not
        # state-entry time: a 2110 while already lost does not move `since`).
        codes = [
            (getattr(c, "readiness_event_at", 0) or 0, getattr(c, "readiness_code", None))
            for c in group
            if isinstance(getattr(c, "readiness_code", None), int)
        ]
        return {
            "state": state,
            "since": datetime.fromtimestamp(min(sinces), timezone.utc).isoformat() if sinces else None,
            "last_code": max(codes)[1] if codes else None,
        }

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
                "fenced": self.is_fenced(role),
            }
            for role in POOL_ROLES
        }


class _PoolContext:
    """Async context manager for exclusive role access."""

    def __init__(self, pool: IBPool, role: str):
        self._pool = pool
        self._role = role

    async def __aenter__(self) -> IBClient:
        if self._pool.is_fenced(self._role):
            raise IBRoleFenced(f"IB pool: {self._role} is busy with an abandoned call")
        lock = self._pool._locks[self._role]
        await lock.acquire()
        try:
            return await self._enter_locked()
        except BaseException:
            # Includes CancelledError (a caller deadline during a reconnect):
            # the lock must never outlive the caller.
            lock.release()
            raise

    async def _enter_locked(self) -> IBClient:
        if self._pool.is_fenced(self._role):
            raise IBRoleFenced(f"IB pool: {self._role} is busy with an abandoned call")
        # Auto-reconnect if connection dropped — never for ``orders``: only
        # the order owner's supervisor replaces that connection (A2).
        if not self._pool.is_connected(self._role):
            if self._role == ORDERS_ROLE:
                raise ConnectionError(f"IB pool: {self._role} is not connected")
            reconnected = await self._pool._reconnect(self._role)
            if not reconnected:
                raise ConnectionError(f"IB pool: {self._role} is not connected")

        client = self._pool.get(self._role)
        if client is None:
            raise ConnectionError(f"IB pool: {self._role} has no client")

        return client

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self._pool._locks[self._role].release()
        return False
