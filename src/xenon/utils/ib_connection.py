"""Legacy IB connection helpers kept for backward-compatible scripts/tests.

New code should prefer ``clients.ib_client.IBClient``.
"""

from __future__ import annotations

import os
from typing import Optional

from ib_async import IB

from xenon.api.trading_mode import EXPECTED_PORT as _EXPECTED_PORT

DEFAULT_HOST = os.environ.get("IB_GATEWAY_HOST", "127.0.0.1")
DEFAULT_GATEWAY_PORT = _EXPECTED_PORT
DEFAULT_TWS_PORT = 7497

# ---------------------------------------------------------------------------
# Test-session broker boundary guard
# ---------------------------------------------------------------------------
# While XENON_TEST_BROKER_GUARD=1 is in the environment — set by the repo-root
# pytest conftest for the whole test session and inherited by every spawned
# child — every broker connect must pass this allowlist: only the exact
# host:port in XENON_TEST_BROKER_ALLOW may be dialed (the root conftest sets
# it to "127.0.0.1:4002" only under the `paper_ib` marker). With the env var
# unset this function is a pure no-op, so production behavior is unchanged.
# `live_ib` tests remove the guard env — the explicit `-m live_ib` operator
# opt-in — and therefore connect unguarded.
TEST_BROKER_GUARD_ENV = "XENON_TEST_BROKER_GUARD"
TEST_BROKER_ALLOW_ENV = "XENON_TEST_BROKER_ALLOW"


class BrokerEndpointDenied(ConnectionRefusedError):
    """Test-session broker guard refused an IB connection.

    Only ever raised while ``XENON_TEST_BROKER_GUARD=1``. A
    ``ConnectionRefusedError`` so health probes that catch ``OSError`` see
    the same verdict as an unreachable gateway, while the message names the
    allowlisted endpoint and the opting-in marker.
    """


def assert_broker_endpoint_allowed(host: str, port: int) -> None:
    """Refuse any broker endpoint outside the test-session allowlist.

    Single choke point routed through by every ``ib_async`` connect call
    site (``IBClient.connect``/reconnect, ``connect_ib`` here and in
    ``reports.portfolio_report``). No-op when the guard env is unset.
    """
    if os.environ.get(TEST_BROKER_GUARD_ENV) != "1":
        return
    allow = os.environ.get(TEST_BROKER_ALLOW_ENV)
    if allow:
        allow_host, sep, allow_port = allow.rpartition(":")
        if sep and (allow_host, allow_port) == (str(host), str(int(port))):
            return
    raise BrokerEndpointDenied(
        f"test-session broker guard refused IB connect to {host}:{port} — "
        f"XENON_TEST_BROKER_ALLOW={allow!r} is the only permitted endpoint "
        "(set to '127.0.0.1:4002' only under the paper_ib marker); "
        "mark the test live_ib for an explicitly opted-in real connection"
    )

# Legacy registry values preserved exactly for compatibility tests.
CLIENT_IDS = {
    "ib_order_manage": 0,
    "ib_sync": 0,
    "ib_order": 2,
    "ib_orders": 11,
    "ib_fill_monitor": 52,
    "exit_order_service": 60,
    "ib_reconcile": 0,
    "fetch_analyst_ratings": 99,
    "ib_realtime_server": 100,
}


def connect_ib(
    client_name: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_GATEWAY_PORT,
    client_id: Optional[int] = None,
    timeout: int = 10,
):
    """Legacy raw-IB connector used by older scripts/tests."""
    if client_name not in CLIENT_IDS:
        raise ValueError(f"Unknown client name: {client_name}")

    ib = IB()
    resolved_client_id = CLIENT_IDS[client_name] if client_id is None else client_id
    assert_broker_endpoint_allowed(host, port)
    ib.connect(host, port, clientId=resolved_client_id, timeout=timeout)
    return ib
