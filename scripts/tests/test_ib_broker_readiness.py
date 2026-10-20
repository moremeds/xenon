"""PR-10 / A4 — broker readiness per IB connection, separate from the socket.

unknown at start and after every socket replacement; ready only after an
open-orders round trip completes (or 1102); lost on 1100 / 2110; 1101 drops
back to unknown until a fresh round trip. Fakes only — no IB.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from xenon.clients.ib_client import (
    READINESS_LOST,
    READINESS_READY,
    READINESS_UNKNOWN,
    IBClient,
)

pytestmark = pytest.mark.usefixtures("bypass_broker_boundary")


def _connected_client() -> IBClient:
    client = IBClient()
    client._ib = MagicMock()
    client._ib.isConnected.return_value = True
    client._ib.reqAllOpenOrders.return_value = []
    client._ib.run.return_value = []
    client.connect(client_id=91, timeout=1)
    return client


def _event(client: IBClient, code: int) -> None:
    client._on_error(-1, code, f"code {code}")


def test_new_and_freshly_connected_clients_are_unknown():
    assert IBClient().broker_readiness == READINESS_UNKNOWN
    assert _connected_client().broker_readiness == READINESS_UNKNOWN


def test_open_socket_alone_is_not_readiness():
    """reqCurrentTime / isConnected never promote (observed outage behaviour)."""
    client = _connected_client()
    client._ib.reqCurrentTime.return_value = 0
    assert client.is_connected()
    assert client.broker_readiness == READINESS_UNKNOWN


def test_completed_open_orders_round_trip_promotes_to_ready():
    client = _connected_client()
    assert client.confirm_broker_ready(timeout=0.1) == READINESS_READY
    client._ib.reqAllOpenOrdersAsync.assert_called_once()
    client._ib.reqCurrentTime.assert_not_called()


def test_get_open_orders_round_trip_also_counts():
    client = _connected_client()
    client.get_open_orders()
    assert client.broker_readiness == READINESS_READY


def test_startup_during_outage_stays_unknown():
    """Round trip times out (gateway up, IB unreachable) → not ready."""
    client = _connected_client()
    client._ib.run.side_effect = TimeoutError()
    assert client.confirm_broker_ready(timeout=0.1) == READINESS_UNKNOWN


@pytest.mark.parametrize("code", [1100, 2110])
def test_loss_codes_mark_lost(code):
    client = _connected_client()
    client.confirm_broker_ready()
    _event(client, code)
    assert client.broker_readiness == READINESS_LOST


def test_lost_is_not_cleared_by_a_round_trip_alone():
    client = _connected_client()
    _event(client, 1100)
    client.confirm_broker_ready()
    client.get_open_orders()
    assert client.broker_readiness == READINESS_LOST


def test_1102_restores_ready():
    client = _connected_client()
    _event(client, 1100)
    _event(client, 1102)
    assert client.broker_readiness == READINESS_READY


def test_1101_needs_a_fresh_round_trip():
    client = _connected_client()
    _event(client, 1100)
    _event(client, 1101)
    assert client.broker_readiness == READINESS_UNKNOWN
    client.confirm_broker_ready()
    assert client.broker_readiness == READINESS_READY


def test_event_during_round_trip_wins_over_its_result():
    """A 1100 processed while the probe runs must not be overwritten by ready."""
    client = _connected_client()

    def run_with_outage(*_a, **_k):
        _event(client, 1100)
        return []

    client._ib.run.side_effect = run_with_outage
    assert client.confirm_broker_ready() == READINESS_LOST

    client2 = _connected_client()

    def orders_with_1101(*_a, **_k):
        _event(client2, 1101)
        return []

    client2._ib.reqAllOpenOrders.side_effect = orders_with_1101
    client2.get_open_orders()
    assert client2.broker_readiness == READINESS_UNKNOWN


def test_socket_replacement_and_disconnect_reset_stale_state():
    client = _connected_client()
    client.confirm_broker_ready()
    assert client.broker_readiness == READINESS_READY
    client.connect(client_id=91, timeout=1)  # replacement socket
    assert client.broker_readiness == READINESS_UNKNOWN

    client.confirm_broker_ready()
    client.disconnect()
    assert client.broker_readiness == READINESS_UNKNOWN

    client._ib.isConnected.return_value = False
    client._ib.run.reset_mock()
    assert client.confirm_broker_ready() == READINESS_UNKNOWN
    client._ib.run.assert_not_called()


def test_auto_connect_also_resets():
    client = _connected_client()
    client.confirm_broker_ready()
    client.connect(client_id="auto", timeout=1)
    assert client.broker_readiness == READINESS_UNKNOWN


# -- F3 (Astra T1–T3): a failed round trip never returns READY --------------


def test_failed_round_trip_after_ready_returns_unknown_and_keeps_state():
    """Ready once, then the probe times out: no evidence → UNKNOWN to the
    caller. The stored state is not promoted and not rewritten."""
    client = _connected_client()
    assert client.confirm_broker_ready() == READINESS_READY
    generation = client.readiness_generation
    client._ib.run.side_effect = TimeoutError("probe timed out")
    assert client.confirm_broker_ready(timeout=0.01) == READINESS_UNKNOWN
    assert client.readiness_generation == generation


def test_failed_round_trip_keeps_lost():
    client = _connected_client()
    _event(client, 1100)
    client._ib.run.side_effect = TimeoutError("probe timed out")
    assert client.confirm_broker_ready(timeout=0.01) == READINESS_LOST
    assert client.broker_readiness == READINESS_LOST


def test_1102_then_failed_round_trip_is_not_ready():
    client = _connected_client()
    _event(client, 1100)
    _event(client, 1102)
    assert client.broker_readiness == READINESS_READY
    client._ib.run.side_effect = ConnectionError("socket closed")
    assert client.confirm_broker_ready(timeout=0.01) == READINESS_UNKNOWN


def test_1102_during_round_trip_is_not_this_calls_evidence():
    client = _connected_client()

    def run_with_restore(*_a, **_k):
        _event(client, 1100)
        _event(client, 1102)
        return []

    client._ib.run.side_effect = run_with_restore
    assert client.confirm_broker_ready() == READINESS_UNKNOWN


def test_ready_then_socket_loss_is_not_ready():
    client = _connected_client()
    assert client.confirm_broker_ready() == READINESS_READY
    client._ib.isConnected.return_value = False
    assert client.confirm_broker_ready() == READINESS_UNKNOWN
