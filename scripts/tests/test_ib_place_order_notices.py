"""xenon-ib-place-order decides by order state, not by "any errorEvent".

Live incident 2026-10-17 (Saturday): a BUY 1 SPY BAG was accepted by IB
(PreSubmitted, permId set, visible in reqAllOpenOrders) but the CLI returned
"error" because IB sent a 399 "Order Message: Warning: ..." during the ack
wait. The place route then marked the reservation REJECTED while the order
was live at IB — orphan order, retry → duplicate order.

Fake IB only — no network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from xenon.execution import ib_place_order

_ORDER_ID = 7


class _ErrorEvent:
    def __init__(self):
        self.handlers: list = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class _FakeClient:
    """Places one order; emits ``events`` during the ack wait (``sleep``),
    then reports ``final_status`` / ``final_perm_id`` — the ib_async order
    of operations (placeOrder assigns orderId, then IB messages arrive)."""

    events: list[tuple[int, int, str]] = []
    final_status = "PreSubmitted"
    final_perm_id = 0

    def __init__(self):
        self._ib = SimpleNamespace(errorEvent=_ErrorEvent())
        self._trade = None

    def connect(self, **_kwargs):
        return None

    def qualify_contracts(self, *contracts):
        for idx, contract in enumerate(contracts, start=1):
            contract.conId = idx
        return list(contracts)

    def place_order(self, _contract, order):
        order.orderId = _ORDER_ID
        self._trade = SimpleNamespace(
            order=SimpleNamespace(orderId=_ORDER_ID, permId=0, tif=order.tif),
            orderStatus=SimpleNamespace(status="PendingSubmit"),
        )
        return self._trade

    def sleep(self, _seconds):
        cls = self.__class__
        for req_id, code, msg in cls.events:
            for handler in self._ib.errorEvent.handlers:
                handler(req_id, code, msg, None)
        self._trade.order.permId = cls.final_perm_id
        self._trade.orderStatus.status = cls.final_status

    def disconnect(self):
        return None


@pytest.fixture
def cli(monkeypatch):
    _FakeClient.events = []
    _FakeClient.final_status = "PreSubmitted"
    _FakeClient.final_perm_id = 0
    monkeypatch.setattr(ib_place_order, "IBClient", _FakeClient)
    return ib_place_order


_COMBO = {
    "type": "combo",
    "symbol": "SPY",
    "action": "BUY",
    "quantity": 1,
    "limitPrice": 0.05,
    "account": "U2222222",
    "legs": [
        {"expiry": "20261120", "strike": 500, "right": "C", "action": "BUY"},
        {"expiry": "20261120", "strike": 510, "right": "C", "action": "SELL"},
    ],
}
_STOCK = {"type": "stock", "symbol": "SPY", "action": "BUY", "quantity": 1, "limitPrice": 1.0, "account": "U2222222"}

_W399 = "Order Message:\nBUY 1 SPY BAG\nWarning: your order will not be placed at the exchange until 2026-10-19 09:30:00 US/Eastern"


def test_399_warning_on_live_presubmitted_order_is_ok(cli):
    _FakeClient.events = [(_ORDER_ID, 399, _W399)]
    _FakeClient.final_status = "PreSubmitted"
    _FakeClient.final_perm_id = 123456

    result = cli.place_order(dict(_COMBO))

    assert result["status"] == "ok"
    assert result["orderId"] == _ORDER_ID
    assert result["permId"] == 123456
    assert result["initialStatus"] == "PreSubmitted"
    assert result["ibNotices"] == [{"reqId": _ORDER_ID, "code": 399, "message": _W399}]


def test_399_warning_sets_validation_error_status_still_ok(cli):
    # ib_async's wrapper.error() sets orderStatus.status = ValidationError on a
    # warning for a known trade — the order is still live at the broker.
    _FakeClient.events = [(_ORDER_ID, 399, _W399)]
    _FakeClient.final_status = "ValidationError"
    _FakeClient.final_perm_id = 123456

    result = cli.place_order(dict(_COMBO))

    assert result["status"] == "ok"
    assert result["initialStatus"] == "ValidationError"


def test_connection_wide_notice_with_req_id_minus_one_is_ok(cli):
    _FakeClient.events = [
        (-1, 2110, "Connectivity between TWS and server is broken. It will be restored automatically.")
    ]
    _FakeClient.final_status = "Submitted"
    _FakeClient.final_perm_id = 555

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "ok"
    assert result["ibNotices"][0]["reqId"] == -1
    assert result["ibNotices"][0]["code"] == 2110


def test_other_req_id_hard_error_does_not_reject_this_order(cli):
    _FakeClient.events = [(99, 200, "No security definition has been found for the request")]
    _FakeClient.final_status = "Submitted"
    _FakeClient.final_perm_id = 555

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "ok"
    assert result["ibNotices"][0]["reqId"] == 99


def test_order_scoped_201_rejection_with_cancelled_status_is_error(cli):
    _FakeClient.events = [(_ORDER_ID, 201, "Order rejected - reason: insufficient buying power")]
    _FakeClient.final_status = "Cancelled"

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "error"
    assert result["code"] == 201
    assert "insufficient buying power" in result["message"]
    assert result["initialStatus"] == "Cancelled"


def test_order_scoped_hard_error_without_any_status_is_error(cli):
    _FakeClient.events = [(_ORDER_ID, 201, "Order rejected - reason: x")]
    _FakeClient.final_status = ""  # no orderStatus ever arrived, permId 0

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "error"
    assert result["code"] == 201


def test_terminal_inactive_without_error_event_is_error(cli):
    _FakeClient.final_status = "Inactive"

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "error"
    assert result["initialStatus"] == "Inactive"


def test_code_110_still_returns_code_for_limit_off_tick(cli):
    _FakeClient.events = [
        (_ORDER_ID, 110, "The price does not conform to the minimum price variation for this contract.")
    ]
    _FakeClient.final_status = "Cancelled"

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "error"
    assert result["code"] == 110


def test_code_435_still_maps_to_account_unresolved(cli):
    _FakeClient.events = [(_ORDER_ID, 435, "You must specify an account.")]
    _FakeClient.final_status = "Cancelled"

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "error"
    assert result["reason_code"] == "ACCOUNT_UNRESOLVED"


def test_filled_immediately_is_ok(cli):
    _FakeClient.final_status = "Filled"
    _FakeClient.final_perm_id = 9

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "ok"


def test_connection_loss_before_ack_does_not_reject(cli):
    # No ack yet (no status, permId 0) and only a connection-wide reqId -1
    # message: nothing rejected THIS order, so it is not an error.
    _FakeClient.events = [(-1, 1100, "Connectivity between IB and Trader Workstation has been lost.")]
    _FakeClient.final_status = ""

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "ok"
    assert result["ibNotices"][0]["code"] == 1100


def test_order_scoped_warning_before_ack_does_not_reject(cli):
    _FakeClient.events = [(_ORDER_ID, 399, _W399)]
    _FakeClient.final_status = ""

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "ok"


def test_order_scoped_non_notice_code_on_live_order_is_ok(cli):
    # State decides: any order-scoped message on an order IB reports live
    # is carried as a notice, not a rejection.
    _FakeClient.events = [(_ORDER_ID, 10349, "order-scoped message")]
    _FakeClient.final_status = "Submitted"
    _FakeClient.final_perm_id = 42

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "ok"
    assert result["ibNotices"][0]["code"] == 10349
