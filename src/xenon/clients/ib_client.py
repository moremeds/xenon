"""Comprehensive Interactive Brokers API client.

Wraps ``ib_async.IB`` with connection management, order operations,
market data, portfolio queries, fill monitoring, and Flex Query support.

Usage::

    from xenon.clients.ib_client import IBClient

    with IBClient() as client:
        client.connect(client_name="ib_sync")
        positions = client.get_positions()

    # Or without context manager
    client = IBClient()
    client.connect(host="127.0.0.1", port=4001, client_id=1)
    try:
        orders = client.get_open_orders()
    finally:
        client.disconnect()
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from dotenv import load_dotenv
from ib_async import IB, FlexReport

from xenon.api.trading_mode import EXPECTED_PORT as _EXPECTED_PORT
from xenon.utils.ib_connection import assert_broker_endpoint_allowed

# Load root .env so IB_GATEWAY_HOST is available before DEFAULT_HOST is computed
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class IBError(Exception):
    """Base exception for all IB client errors."""


class IBConnectionError(IBError):
    """Raised when an IB connection cannot be established or is lost."""


class IBOrderError(IBError):
    """Raised when an order operation fails."""


class IBOrderAccountError(IBOrderError):
    """A new order has no usable account; it was NOT sent to IB.

    ``reason_code`` is ``ACCOUNT_UNRESOLVED`` (empty / ``legacy_unknown``) or
    ``ACCOUNT_NOT_MANAGED`` (not in ``ib.managedAccounts()``).
    """

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code


class IBTimeoutError(IBError):
    """Raised when an operation times out."""


class IBContractError(IBError):
    """Raised when contract qualification or lookup fails."""


class ClientIdNotAllowed(IBConnectionError):
    """The requested clientId is outside the caller's zone; no connect was tried.

    Raised by :func:`resolve_client_id` (and so by ``IBClient.connect`` and
    every reconnect that goes through it) before any socket is opened.
    """


# ---------------------------------------------------------------------------
# Constants — re-exported from ib_connection for backward compat
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Client ID Range Allocation
# ---------------------------------------------------------------------------
# IB allows client IDs 0-999. Each environment that shares a gateway gets its
# own block of 100 ids starting at ``XENON_IB_CLIENT_ID_BASE`` (0-800, step
# 100; prod = 0 on the Mac mini, dev = 100 via ``scripts/infra/dev.sh``). The
# zones below are OFFSETS inside that block, so persistent services and
# on-demand scripts never collide, inside one environment or across two:
#
#   +0-9     Pool (persistent connections held by FastAPI IBPool) — only the
#            pool may open these (``pool_role=``)
#   +10-19   WS relay (persistent, ib_realtime_server.js) — never from Python
#   +20-49   Subprocess scripts (on-demand, ``client_id="auto"``)
#   +50-69   Scanners (CRI/VCG ad-hoc pools)
#   +70-89   Daemons (fill monitor, exit service)
#   +90-99   Standalone CLI (manual runs)
#
# Ids 900-999 belong to no environment (raw probe harnesses only). The
# allocation check in ``IBClient.connect`` is an operational contract, not
# fencing: two processes that share a base can still collide.
POOL_ID_RANGE = (0, 9)
RELAY_ID_RANGE = (10, 19)
SUBPROCESS_ID_RANGE = (20, 49)

CLIENT_ID_BASE_ENV = "XENON_IB_CLIENT_ID_BASE"
CLIENT_ID_BASE_STEP = 100
CLIENT_ID_BASE_MAX = 800

# Pool roles — persistent connections held by FastAPI IBPool (offsets).
# IDs 3-5 avoid collisions with stale clientId 0 reservations after unclean restarts
POOL_ROLES: dict = {
    "sync": 3,
    "orders": 4,
    "data": 5,
}

# Registry of named non-pool callers (offsets from the base). Names that map
# into the pool or relay zone are refused by the allocation check.
CLIENT_IDS: dict = {
    "ib_order_manage": 20,  # subprocess range (auto-allocate preferred)
    "ib_sync": 3,  # pool role
    "ib_orders": 4,  # pool role
    "ib_reconcile": 21,  # subprocess range
    "ib_order": 22,  # subprocess range
    "ib_fill_monitor": 70,  # daemon range
    "exit_order_service": 71,  # daemon range
    "fetch_analyst_ratings": 90,  # standalone CLI range
    "ib_place_order": 24,  # subprocess range
    "ib_option_chain": 27,  # subprocess range (/options/chain, /options/expirations)
    "vcg_scanner": 50,  # scanner range
    "cri_scanner": 5,  # pool data role
    "ib_realtime_server": 10,  # relay range
    "option_chain_snapshotter_a": 95,  # daemon range — pool A for option_chain archive snapshotter
    "option_chain_snapshotter_b": 96,  # daemon range — pool B for option_chain archive snapshotter
}


def client_id_base() -> int:
    """Return this environment's clientId base from ``XENON_IB_CLIENT_ID_BASE``.

    Read at call time. Unset or empty means 0 (prod). Anything that is not a
    multiple of 100 in 0-800 raises :class:`ClientIdNotAllowed`.
    """
    raw = (os.environ.get(CLIENT_ID_BASE_ENV) or "").strip()
    if not raw:
        return 0
    try:
        base = int(raw)
    except ValueError:
        raise ClientIdNotAllowed(f"{CLIENT_ID_BASE_ENV}={raw!r} is not an integer") from None
    if base < 0 or base > CLIENT_ID_BASE_MAX or base % CLIENT_ID_BASE_STEP:
        raise ClientIdNotAllowed(
            f"{CLIENT_ID_BASE_ENV}={base} is invalid — use 0-{CLIENT_ID_BASE_MAX} in steps of {CLIENT_ID_BASE_STEP}"
        )
    return base


def pool_client_id(role: str) -> int:
    """Absolute clientId of a pool role in this environment (base + offset)."""
    if role not in POOL_ROLES:
        raise ValueError(f"Unknown pool role: {role}. Valid: {list(POOL_ROLES.keys())}")
    return client_id_base() + POOL_ROLES[role]


def resolve_client_id(client_id: int, *, pool_role: Optional[str] = None) -> int:
    """Check an explicit clientId against this environment's zones.

    - ``pool_role`` given: the id must be exactly that role's id. Only
      ``xenon.api.ib_pool`` passes ``pool_role``.
    - otherwise the id must lie in ``base+20 .. base+99``: never the pool
      zone (+0-9, which includes the master clientId 0 for base 0), never
      the relay zone (+10-19), never another environment's block.

    Raises :class:`ClientIdNotAllowed`; returns the id unchanged.
    """
    if isinstance(client_id, bool) or not isinstance(client_id, int):
        raise ClientIdNotAllowed(f"clientId {client_id!r} is not an integer")
    base = client_id_base()
    if pool_role is not None:
        expected = pool_client_id(pool_role)
        if client_id != expected:
            raise ClientIdNotAllowed(
                f"pool role {pool_role!r} must use clientId {expected} (base {base}), not {client_id}"
            )
        return client_id
    offset = client_id - base
    if not 0 <= offset <= 99:
        raise ClientIdNotAllowed(
            f"clientId {client_id} is outside this environment's block {base}-{base + 99} ({CLIENT_ID_BASE_ENV}={base})"
        )
    if offset <= POOL_ID_RANGE[1]:
        raise ClientIdNotAllowed(
            f"clientId {client_id} is in the pool zone {base}-{base + 9}; only the API pool may use it"
        )
    if offset <= RELAY_ID_RANGE[1]:
        raise ClientIdNotAllowed(
            f"clientId {client_id} is in the relay zone {base + 10}-{base + 19}; Python never opens relay ids"
        )
    return client_id


# ---------------------------------------------------------------------------
# Broker readiness (A4)
# ---------------------------------------------------------------------------
# An open socket is not proof that the gateway can reach IB: during an outage
# reqCurrentTime still answers locally and open-orders times out (observed
# 2026-10-20). Each connection therefore tracks:
#   unknown — after every (re)connect and after 1101, until a broker round trip
#             (an open-orders request that returns) completes;
#   ready   — round trip seen, or 1102 (restored, data maintained);
#   lost    — 1100 (IB↔gateway lost) or 2110 (gateway↔server broken).
# ib_async only processes these events while the connection's event loop runs,
# so the value can lag until the next call on that connection.
READINESS_UNKNOWN = "unknown"
READINESS_READY = "ready"
READINESS_LOST = "lost"

_READINESS_LOST_CODES = frozenset({1100, 2110})
_READINESS_RESTORED_CODE = 1102
_READINESS_RECHECK_CODE = 1101

DEFAULT_READINESS_PROBE_TIMEOUT_S = 5.0


DEFAULT_HOST = os.environ.get("IB_GATEWAY_HOST", "127.0.0.1")
DEFAULT_GATEWAY_PORT = _EXPECTED_PORT
DEFAULT_TWS_PORT = 7497

# IB error codes that are informational / non-critical
_INFO_CODES = frozenset(
    {
        2104,  # Market data farm connection is OK
        2106,  # HMDS data farm connection is OK
        2108,  # Market data farm connection is inactive
        2158,  # Sec-def data farm connection is OK
    }
)

# IB error codes that should be silently ignored (not user-relevant)
_IGNORE_CODES = frozenset(
    {
        10358,  # Reuters Fundamentals subscription inactive — auto-fallback
    }
)

# IB error codes indicating connectivity issues
_CONNECTIVITY_CODES = frozenset(
    {
        1100,  # Connectivity between IB and TWS has been lost
        1101,  # Connectivity restored — data lost
        1102,  # Connectivity restored — data maintained
    }
)

# IB error codes for pacing violations — retry with exponential backoff
_PACING_CODES = frozenset(
    {
        162,  # Historical market data pacing violation
        366,  # No historical data query found for ticker id
    }
)

# Max retries per reqId for pacing violations
_MAX_PACING_RETRIES = 3

# IB error codes for invalid contracts — don't retry
_INVALID_CONTRACT_CODES = frozenset(
    {
        200,  # No security definition has been found
        354,  # Requested market data is not subscribed
    }
)

# Reconnection constants
MAX_RECONNECT_ATTEMPTS = 5
MAX_RECONNECT_BACKOFF = 30  # seconds

logger = logging.getLogger("ib_client")


# ---------------------------------------------------------------------------
# IBClient
# ---------------------------------------------------------------------------


class IBClient:
    """High-level Interactive Brokers API client.

    Wraps ``ib_async.IB`` with:
    - Connection lifecycle (connect / disconnect / reconnect)
    - Context manager support
    - Client ID registry lookup
    - Portfolio, order, market-data, execution, and Flex Query operations
    - Structured logging
    - Retry logic for transient connection errors
    - Graceful handling of known IB error codes
    """

    def __init__(self) -> None:
        self._ib = IB()
        self.logger = logging.getLogger("ib_client")
        self._last_host: str = DEFAULT_HOST
        self._last_port: int = DEFAULT_GATEWAY_PORT
        self._last_client_id: int = 0
        self._last_timeout: int = 10
        self._last_error: Optional[tuple] = None

        # Subscription tracking for recovery after disconnect
        self._subscriptions: List[Dict[str, Any]] = []

        # Reconnection state
        self._reconnecting: bool = False

        # Pacing violation retry tracking (reqId -> count)
        self._pacing_retries: Dict[int, int] = {}

        # Invalid contracts — callers can check before requesting
        self._failed_contracts: Set[Any] = set()

        # Broker readiness (A4). The generation changes on every readiness
        # event so a round trip that started before an event never promotes.
        self._pool_role: Optional[str] = None
        self._readiness: str = READINESS_UNKNOWN
        self._readiness_generation: int = 0

        # Wire up error callback
        self._ib.errorEvent += self._on_error

    # -- properties ---------------------------------------------------------

    @property
    def ib(self) -> IB:
        """Return the underlying ``ib_async.IB`` instance."""
        return self._ib

    @property
    def failed_contracts(self) -> Set[Any]:
        """Return the set of contracts that returned invalid from IB."""
        return self._failed_contracts

    # -- connection lifecycle -----------------------------------------------

    def connect(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_GATEWAY_PORT,
        client_id: Optional[Union[int, str]] = None,
        client_name: Optional[str] = None,
        timeout: int = 3,
        max_retries: int = 1,
        pool_role: Optional[str] = None,
    ) -> None:
        """Connect to TWS / IB Gateway.

        Every id goes through :func:`resolve_client_id` before a socket is
        opened (A5 allocation check): ``"auto"`` stays in ``base+20..49``,
        named and explicit ids must sit in ``base+20..99``, and pool role ids
        are only reachable with ``pool_role`` (used by ``xenon.api.ib_pool``).

        Args:
            host: IB Gateway / TWS host.
            port: IB Gateway / TWS port.
            client_id: Explicit client ID (int), ``"auto"`` for range-based
                allocation from :data:`SUBPROCESS_ID_RANGE`, or *None*.
            client_name: Look up the client ID offset in ``CLIENT_IDS``
                (resolved as ``base + offset``).
            timeout: Connection timeout in seconds (default 3s for fast
                fallback when Gateway is unreachable).
            max_retries: Number of attempts before giving up (applies per-ID
                for explicit IDs; ignored for ``"auto"`` which retries across
                the full range).

        Raises:
            ValueError: If *client_name* is not in the registry and no
                *client_id* override is given.
            ClientIdNotAllowed: If the id is outside the caller's zone.
            IBConnectionError: If the connection cannot be established.
        """
        # Test-session broker boundary allowlist — no-op outside pytest
        # (XENON_TEST_BROKER_GUARD unset in production). Covers this path,
        # the "auto" allocation loop, and reconnects (which call connect()).
        assert_broker_endpoint_allowed(host, port)
        # Resolve client ID
        if client_id == "auto":
            if pool_role is not None:
                raise ClientIdNotAllowed("pool roles use a fixed clientId, never 'auto'")
            return self._connect_auto_allocate(host, port, timeout)

        if client_id is None and pool_role is not None:
            client_id = pool_client_id(pool_role)
        elif client_id is None and client_name is not None:
            if client_name not in CLIENT_IDS:
                raise ValueError(f"Unknown client name '{client_name}'. Known names: {sorted(CLIENT_IDS)}")
            client_id = client_id_base() + CLIENT_IDS[client_name]
        elif client_id is None:
            client_id = 0
        client_id = resolve_client_id(client_id, pool_role=pool_role)
        self._pool_role = pool_role

        self._last_host = host
        self._last_port = port
        self._last_client_id = client_id
        self._last_timeout = timeout

        attempt = 0
        last_exc: Optional[Exception] = None
        while attempt < max_retries:
            attempt += 1
            try:
                self._ib.connect(host, port, clientId=client_id, timeout=timeout)
                self._reset_readiness()
                self.logger.info(
                    "Connected to IB on %s:%s (clientId=%s)",
                    host,
                    port,
                    client_id,
                )
                return
            except Exception as exc:
                last_exc = exc
                self.logger.warning(
                    "Connection attempt %d/%d failed: %s",
                    attempt,
                    max_retries,
                    exc,
                )
                if attempt < max_retries:
                    time.sleep(min(attempt, 5))

        raise IBConnectionError(f"Failed to connect to IB on {host}:{port} after {max_retries} attempt(s): {last_exc}")

    def _connect_auto_allocate(
        self,
        host: str,
        port: int,
        timeout: int,
    ) -> None:
        """Try each ID in ``base + SUBPROCESS_ID_RANGE`` from a random offset.

        On 'client id is already in use' errors, rotates to the next ID.
        Non-conflict errors abort immediately.
        """
        import random

        base = client_id_base()
        lo, hi = base + SUBPROCESS_ID_RANGE[0], base + SUBPROCESS_ID_RANGE[1]
        range_size = hi - lo + 1
        start = random.randint(lo, hi)

        for i in range(range_size):
            cid = lo + (start - lo + i) % range_size
            try:
                self._ib.connect(host, port, clientId=cid, timeout=timeout)
                self._reset_readiness()
                self._pool_role = None
                self._last_host = host
                self._last_port = port
                self._last_client_id = cid
                self._last_timeout = timeout
                self.logger.info(
                    "Connected to IB on %s:%s (clientId=%s, auto-allocated)",
                    host,
                    port,
                    cid,
                )
                return
            except Exception as exc:
                if "client id is already in use" in str(exc).lower():
                    self.logger.debug(
                        "Client ID %d in use, trying next",
                        cid,
                    )
                    continue
                # Non-conflict error — don't rotate, just fail
                raise IBConnectionError(f"Failed to connect to IB on {host}:{port} after 1 attempt(s): {exc}") from exc

        raise IBConnectionError(f"Failed to connect to IB on {host}:{port}: all client IDs {lo}-{hi} in use")

    def disconnect(self) -> None:
        """Disconnect from IB. Safe to call when not connected."""
        self._reset_readiness()
        if self._ib.isConnected():
            self._ib.disconnect()
            self.logger.info("Disconnected from IB")
        else:
            self.logger.debug("disconnect() called but already disconnected")

    def is_connected(self) -> bool:
        """Return ``True`` if connected to IB."""
        return self._ib.isConnected()

    # -- context manager ----------------------------------------------------

    def __enter__(self) -> "IBClient":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.disconnect()

    # -- connection guard ---------------------------------------------------

    def _require_connection(self) -> None:
        """Raise if not connected."""
        if not self.is_connected():
            raise IBConnectionError("Not connected to IB. Call connect() first.")

    # -- error handling -----------------------------------------------------

    def _on_error(self, reqId: Any, errorCode: Any, errorString: str, contract: Any = None) -> None:
        """Handle IB error/warning callbacks.

        This is wired into ``ib.errorEvent``.
        """
        code = int(errorCode) if errorCode else 0

        if code in _IGNORE_CODES:
            self.logger.debug("IB info %d (ignored): %s", code, errorString)
            return

        if code in _INFO_CODES:
            self.logger.info("IB info %d: %s", code, errorString)
            return

        if code in _CONNECTIVITY_CODES or code in _READINESS_LOST_CODES:
            self.logger.warning("IB connectivity %d: %s", code, errorString)
            self._apply_readiness_event(code)
            return

        # Pacing violations — track per reqId, cap at max retries
        if code in _PACING_CODES:
            rid = int(reqId) if reqId else 0
            current = self._pacing_retries.get(rid, 0)
            if current < _MAX_PACING_RETRIES:
                self._pacing_retries[rid] = current + 1
                self.logger.warning(
                    "IB pacing violation %d (reqId=%s, retry %d/%d): %s",
                    code,
                    rid,
                    current + 1,
                    _MAX_PACING_RETRIES,
                    errorString,
                )
            else:
                self.logger.error(
                    "IB pacing violation %d (reqId=%s) — max retries exhausted: %s",
                    code,
                    rid,
                    errorString,
                )
            return

        # Invalid contracts — add to failed set, no retry
        if code in _INVALID_CONTRACT_CODES:
            if contract is not None:
                self._failed_contracts.add(contract)
            self.logger.warning(
                "IB invalid contract %d (reqId=%s): %s (contract=%s)",
                code,
                reqId,
                errorString,
                contract,
            )
            return

        # Store last error for operations to check
        self._last_error = (code, errorString)
        self.logger.error("IB error %d: %s", code, errorString)

    # -- disconnect recovery ------------------------------------------------

    def _on_disconnect(self) -> None:
        """Handle disconnect: auto-reconnect with exponential backoff and restore subscriptions."""
        if self._reconnecting:
            self.logger.debug("Already reconnecting, skipping concurrent attempt")
            return

        # Same boundary allowlist as connect() — this path calls
        # self._ib.connect directly, so it needs its own check.
        assert_broker_endpoint_allowed(self._last_host, self._last_port)
        # Same allocation check as connect() (A5): a reconnect never reopens
        # an id outside this caller's zone.
        resolve_client_id(self._last_client_id, pool_role=self._pool_role)

        self._reconnecting = True
        self.logger.warning("Disconnected from IB — attempting reconnection")

        try:
            connected = False
            for attempt in range(MAX_RECONNECT_ATTEMPTS):
                delay = min(2**attempt, MAX_RECONNECT_BACKOFF)
                try:
                    self._ib.connect(
                        self._last_host,
                        self._last_port,
                        clientId=self._last_client_id,
                        timeout=self._last_timeout,
                    )
                    self._reset_readiness()
                    self.logger.info(
                        "Reconnected to IB on attempt %d/%d",
                        attempt + 1,
                        MAX_RECONNECT_ATTEMPTS,
                    )
                    connected = True
                    break
                except Exception as exc:
                    self.logger.warning(
                        "Reconnect attempt %d/%d failed: %s — waiting %ds",
                        attempt + 1,
                        MAX_RECONNECT_ATTEMPTS,
                        exc,
                        delay,
                    )
                    time.sleep(delay)

            if not connected:
                self.logger.error(
                    "Failed to reconnect after %d attempts",
                    MAX_RECONNECT_ATTEMPTS,
                )
                return

            # Restore tracked subscriptions
            restored = 0
            failed = 0
            for sub in self._subscriptions:
                try:
                    self._ib.reqMktData(
                        sub["contract"],
                        sub["generic_ticks"],
                        False,
                        False,
                    )
                    restored += 1
                except Exception as exc:
                    failed += 1
                    self.logger.warning(
                        "Failed to restore subscription for %s: %s",
                        sub["contract"],
                        exc,
                    )

            self.logger.info(
                "Subscription restoration complete: %d restored, %d failed",
                restored,
                failed,
            )

        finally:
            self._reconnecting = False

    # -- subscription management --------------------------------------------

    # -- portfolio operations -----------------------------------------------

    def get_positions(self) -> list:
        """Return current positions (``ib.positions()``)."""
        self._require_connection()
        return self._ib.positions()

    def get_pnl(self, account: str = "") -> Any:
        """Request P&L for account. Returns PnL with dailyPnL, unrealizedPnL, realizedPnL."""
        self._require_connection()
        pnl = self._ib.reqPnL(account)
        self._ib.sleep(2)
        return pnl

    def cancel_pnl(self, pnl_obj: Any) -> None:
        """Cancel P&L subscription."""
        if pnl_obj:
            self._ib.cancelPnL(pnl_obj)

    def cancel_pnl_single(self, account: str, con_id: int) -> None:
        """Cancel per-position P&L subscription."""
        try:
            self._ib.cancelPnLSingle(account, "", con_id)
        except Exception:
            pass  # ignore cancel errors

    # -- order operations ---------------------------------------------------

    def _require_managed_account(self, account: Any) -> str:
        """Return ``account`` if this gateway login manages it; else fail closed.

        Never fall back to IB's default account: with several managed accounts
        IB rejects the order (error 435); with one it silently picks it, which
        can differ from the scope stamped on the order row.
        """
        acct = str(account or "").strip()
        if not acct or acct == "legacy_unknown":
            raise IBOrderAccountError(
                "ACCOUNT_UNRESOLVED",
                "Order account is unresolved (empty or legacy_unknown); refusing to place on IB's default account",
            )
        managed = [str(a) for a in (self._ib.managedAccounts() or [])]
        if acct not in managed:
            raise IBOrderAccountError(
                "ACCOUNT_NOT_MANAGED",
                f"Order account {acct!r} is not managed by this IB login ({len(managed)} managed account(s))",
            )
        return acct

    def place_order(self, contract: Any, order: Any) -> Any:
        """Place an order and return the ``Trade`` object.

        A new order (``orderId`` unset) must carry ``order.account`` = the
        scope account, and that account must be in ``managedAccounts()``.
        A modify re-submits IB's own order object (``orderId`` set, account
        already populated by IB) and is passed through unchanged.

        Raises:
            IBOrderAccountError: New order without a managed account. Nothing
                is sent to IB.
            IBOrderError: If the order placement fails.
        """
        self._require_connection()
        if not getattr(order, "orderId", 0):
            order.account = self._require_managed_account(getattr(order, "account", ""))
        try:
            trade = self._ib.placeOrder(contract, order)
            self.logger.info(
                "Placed order: %s %s %s @ %s (orderId=%s)",
                order.action,
                order.totalQuantity,
                contract.symbol if hasattr(contract, "symbol") else contract,
                getattr(order, "lmtPrice", "MKT"),
                trade.order.orderId,
            )
            return trade
        except Exception as exc:
            raise IBOrderError(f"Failed to place order: {exc}") from exc

    def cancel_order(self, order: Any) -> Any:
        """Cancel an open order.

        Args:
            order: The ``Order`` object to cancel.

        Raises:
            IBOrderError: If the cancellation fails.
        """
        self._require_connection()
        try:
            result = self._ib.cancelOrder(order)
            self.logger.info("Cancelled order: orderId=%s", getattr(order, "orderId", "?"))
            return result
        except Exception as exc:
            raise IBOrderError(f"Failed to cancel order: {exc}") from exc

    def get_open_orders(self) -> list:
        """Return all open orders across all clients.

        Returns the trades IB sent in response to this ``reqAllOpenOrders``
        request (collected until ``openOrderEnd``). It does NOT return
        ``openTrades()``: that is ib_async's client-side cache, and on a
        long-lived connection it is not kept in sync for other clients'
        orders. An order that another client cancelled stays there as
        ``Submitted`` forever, so the cache can resurrect a cancelled order
        (incident #35 in docs/reference/order-path-incident-history.md).
        """
        self._require_connection()
        generation = self._readiness_generation
        trades = list(self._ib.reqAllOpenOrders() or [])
        self._note_round_trip(generation)
        return trades

    # -- broker readiness (A4) -----------------------------------------------

    @property
    def broker_readiness(self) -> str:
        """``unknown`` / ``ready`` / ``lost`` — see the module notes (A4)."""
        return self._readiness

    def _set_readiness(self, state: str) -> None:
        self._readiness_generation += 1
        if state != self._readiness:
            self.logger.info("IB broker readiness %s -> %s", self._readiness, state)
        self._readiness = state

    def _reset_readiness(self) -> None:
        """Start and every socket replacement drop stale state to unknown."""
        self._set_readiness(READINESS_UNKNOWN)

    def _apply_readiness_event(self, code: int) -> None:
        if code in _READINESS_LOST_CODES:
            self._set_readiness(READINESS_LOST)
        elif code == _READINESS_RESTORED_CODE:
            self._set_readiness(READINESS_READY)
        elif code == _READINESS_RECHECK_CODE:
            # Restored but data lost: ready only after a fresh round trip.
            self._set_readiness(READINESS_UNKNOWN)

    def _note_round_trip(self, generation: int) -> None:
        """Promote unknown -> ready if no readiness event arrived meanwhile."""
        if generation == self._readiness_generation and self._readiness == READINESS_UNKNOWN:
            self._set_readiness(READINESS_READY)

    def confirm_broker_ready(self, timeout: float = DEFAULT_READINESS_PROBE_TIMEOUT_S) -> str:
        """Run one open-orders round trip and return the resulting readiness.

        Only a completed open-orders request counts as evidence; an open
        socket or ``reqCurrentTime`` does not (the gateway answers those
        locally during an outage). A timeout or error leaves the state as it
        was. Runs on the caller's thread — pool callers use the role executor.
        """
        if not self.is_connected():
            return self._readiness
        generation = self._readiness_generation
        try:
            self._ib.run(self._ib.reqAllOpenOrdersAsync(), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — timeout / socket error: no evidence
            self.logger.warning("IB readiness round trip failed (%s): %r", self._readiness, exc)
            return self._readiness
        self._note_round_trip(generation)
        return self._readiness

    # -- market data --------------------------------------------------------

    def get_quote(self, contract: Any, snapshot: bool = False, generic_ticks: str = "") -> Any:
        """Request market data for a contract and return the ``Ticker``.

        Args:
            contract: The contract to request data for.
            snapshot: If ``True``, request a one-time snapshot.
            generic_ticks: Comma-separated generic tick IDs.
        """
        self._require_connection()
        ticker = self._ib.reqMktData(contract, generic_ticks, snapshot, False)
        if snapshot:
            self._ib.sleep(2)
        else:
            # Track streaming subscription for recovery after disconnect
            self._subscriptions.append(
                {
                    "contract": contract,
                    "generic_ticks": generic_ticks,
                }
            )
        return ticker

    def cancel_market_data(self, contract: Any) -> None:
        """Cancel streaming market data for a contract."""
        self._require_connection()
        self._ib.cancelMktData(contract)

    def set_market_data_type(self, data_type: int) -> None:
        """Set market data type (1=Live, 2=Frozen, 3=Delayed, 4=Delayed-frozen)."""
        self._require_connection()
        self._ib.reqMarketDataType(data_type)

    def get_option_chain(self, symbol: str, exchange: str = "", sec_type: str = "STK") -> list:
        """Return option chain parameters for an underlying.

        Returns a list of ``OptionChain`` objects with expirations, strikes, etc.
        """
        self._require_connection()
        # IB needs underlying conId — qualify first if needed
        return self._ib.reqSecDefOptParams(symbol, exchange, sec_type, 0)

    def qualify_contract(self, contract: Any) -> Any:
        """Qualify a single contract, filling in IB-assigned fields.

        Raises:
            IBContractError: If the contract cannot be qualified.
        """
        self._require_connection()
        results = self._ib.qualifyContracts(contract)
        if not results:
            raise IBContractError(f"Failed to qualify contract: {contract}")
        return results[0]

    def qualify_contracts(self, *contracts: Any) -> list:
        """Qualify multiple contracts in a single call."""
        self._require_connection()
        return self._ib.qualifyContracts(*contracts)

    def qualify_all(self, *contracts: Any) -> list:
        """Qualify every contract or raise ``IBContractError``.

        ib_async 2.x returns a positional list with ``None`` for an unknown
        or ambiguous contract, so a length / truthiness check never fires.
        """
        self._require_connection()
        results = list(self._ib.qualifyContracts(*contracts) or [])
        if len(results) != len(contracts) or any(r is None for r in results):
            missing = [str(c) for c, r in zip(contracts, results + [None] * len(contracts)) if r is None]
            raise IBContractError(f"Could not qualify contract(s): {', '.join(missing)}")
        return results

    def qualify_one(self, contract: Any) -> Any:
        """Qualify one contract or raise ``IBContractError``."""
        return self.qualify_all(contract)[0]

    # -- execution / fill operations ----------------------------------------

    def get_executions(self, exec_filter: Any = None) -> list:
        """Return recent executions, optionally filtered."""
        self._require_connection()
        if exec_filter is not None:
            return self._ib.reqExecutions(exec_filter)
        return self._ib.reqExecutions()

    def get_fills(self) -> list:
        """Return recent fills for this session."""
        self._require_connection()
        return self._ib.fills()

    # -- historical data ----------------------------------------------------

    def get_historical_data(
        self,
        contract: Any,
        duration: str = "1 D",
        bar_size: str = "1 hour",
        what_to_show: str = "TRADES",
        use_rth: bool = True,
        end_date: str = "",
        keep_up_to_date: bool = False,
    ) -> list:
        """Request historical bar data.

        Args:
            contract: The contract to request data for.
            duration: Duration string (e.g. ``"1 D"``, ``"1 W"``, ``"1 M"``).
            bar_size: Bar size setting (e.g. ``"1 hour"``, ``"1 day"``).
            what_to_show: Data type (``TRADES``, ``MIDPOINT``, ``BID``, ``ASK``).
            use_rth: Regular trading hours only.
            end_date: End date/time (empty = now).
            keep_up_to_date: Keep the bars updated.
        """
        self._require_connection()
        return self._ib.reqHistoricalData(
            contract,
            endDateTime=end_date,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=1,
            keepUpToDate=keep_up_to_date,
        )

    # -- contract details ---------------------------------------------------

    def get_contract_details(self, contract: Any) -> list:
        """Return full contract details (``ContractDetails`` list)."""
        self._require_connection()
        return self._ib.reqContractDetails(contract)

    # -- Flex Query ---------------------------------------------------------

    def run_flex_query(self, query_id: int, token: str) -> Any:
        """Execute an IB Flex Query and return the ``FlexReport``.

        Args:
            query_id: The Flex Query ID from IB Account Management.
            token: The Flex Web Service token.

        Returns:
            The ``FlexReport`` object.

        Raises:
            IBError: If the Flex query fails.
        """
        try:
            report = FlexReport(token=token, queryId=query_id)
            self.logger.info("Flex query %d executed successfully", query_id)
            return report
        except Exception as exc:
            raise IBError(f"Flex query {query_id} failed: {exc}") from exc

    # -- utility ------------------------------------------------------------

    def sleep(self, seconds: float) -> None:
        """Sleep while processing IB events (``ib.sleep()``)."""
        self._ib.sleep(seconds)
