"""Single IB connect helper shared by the ib_sync, ib_orders and ib_reconcile CLIs."""

from __future__ import annotations

import sys

from xenon.clients.ib_client import IBClient


def connect_ib(
    host: str,
    port: int,
    client_id: int | str = "auto",
    *,
    exit_on_failure: bool = True,
    quiet: bool = False,
) -> IBClient:
    """Connect to TWS/IB Gateway and return an IBClient.

    On failure the CLI default prints troubleshooting hints and exits 1.
    With ``exit_on_failure=False`` the connect error propagates so the caller
    decides how to degrade. ``quiet`` suppresses the success line.
    """
    client = IBClient()
    try:
        client.connect(host=host, port=port, client_id=client_id)
    except Exception as e:
        if not exit_on_failure:
            raise
        print(f"✗ Connection failed: {e}")
        print("\nTroubleshooting:")
        print("  1. Ensure TWS or IB Gateway is running")
        print("  2. Enable API connections in TWS: Configure > API > Settings")
        print("  3. Check 'Enable ActiveX and Socket Clients'")
        print("  4. Verify port matches (TWS Paper=7497, TWS Live=7496, Gateway=4001)")
        sys.exit(1)
    if not quiet:
        print(f"✓ Connected to IB on {host}:{port}")
    return client
