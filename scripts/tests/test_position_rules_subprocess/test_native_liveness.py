"""Native-order liveness probe. Spec §8, §10.3."""
from __future__ import annotations

from unittest.mock import MagicMock

from xenon.execution.brackets.executor.native_liveness import (
    NativeOrderState,
    verify_native_order_live,
)


def test_returns_filled_when_ib_reports_filled():
    ib = MagicMock()
    ib.get_order_state.return_value = {"status": "Filled", "permId": 12345}
    state = verify_native_order_live(ib_client=ib, perm_id=12345)
    assert state == NativeOrderState.FILLED


def test_returns_cancelled_when_ib_reports_cancelled():
    ib = MagicMock()
    ib.get_order_state.return_value = {"status": "Cancelled", "permId": 12345}
    state = verify_native_order_live(ib_client=ib, perm_id=12345)
    assert state == NativeOrderState.CANCELLED


def test_returns_unknown_on_disconnect():
    ib = MagicMock()
    ib.get_order_state.side_effect = ConnectionError("disconnected")
    state = verify_native_order_live(ib_client=ib, perm_id=12345)
    assert state == NativeOrderState.UNKNOWN
