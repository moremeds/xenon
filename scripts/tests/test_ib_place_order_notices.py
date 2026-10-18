"""xenon-ib-place-order decides by broker acceptance, not by "any errorEvent".

Live incident 2026-10-17 (Saturday): a BUY 1 SPY BAG was accepted by IB
(PreSubmitted, permId set, visible in reqAllOpenOrders) but the CLI returned
"error" because IB sent a warning (399 is the expected cause) during the ack
wait. The place route then marked the reservation REJECTED while the order
was live at IB — orphan order, retry → duplicate order.

Rule under test (``ib_place_order.place_order``):
- accepted := permId > 0 and status not Cancelled / ApiCancelled / Inactive
  (Filled counts) → ok.
- not accepted + an order-scoped non-notice error → error (earliest one).
- not accepted + status ValidationError → error (new-order validation
  failure), even when only "notice" codes such as 321 / 434 arrived.
- not accepted + terminal status → error.
- otherwise (no ack yet: PendingSubmit / ApiPending) → unconfirmed ok.

The client fake drives the REAL installed ib_async ``Wrapper.error`` and
``Wrapper.orderStatus`` against a registered ``Trade`` (the same registration
``IB.placeOrder`` does), so the status transitions — 201 → Cancelled, then a
later 399 → ValidationError — are ib_async's own. No network.
"""

from __future__ import annotations

import pytest
from ib_async import IB, Trade
from ib_async import OrderStatus as IBOrderStatus

from xenon.execution import ib_place_order

_ORDER_ID = 7
_CLIENT_ID = 26


class _WrapperClient:
    """``IBClient`` stand-in backed by a real, unconnected ``ib_async.IB``.

    ``steps`` replay during the ack wait (``sleep``):
    ``("error", reqId, code, msg)`` → ``Wrapper.error``;
    ``("status", status, permId)`` → ``Wrapper.orderStatus``.
    """

    steps: list[tuple] = []
    connected = False

    def __init__(self):
        self._ib = IB()
        self._ib.wrapper.clientId = _CLIENT_ID
        self.trade: Trade | None = None

    def connect(self, **_kwargs):
        self.__class__.connected = True

    def qualify_contracts(self, *contracts):
        for idx, contract in enumerate(contracts, start=1):
            contract.conId = idx
        return list(contracts)

    def place_order(self, contract, order):
        # Mirrors ib_async IB.placeOrder for a new order (ib.py placeOrder).
        order.orderId = _ORDER_ID
        order.clientId = _CLIENT_ID
        status = IBOrderStatus(orderId=_ORDER_ID, status=IBOrderStatus.PendingSubmit)
        self.trade = Trade(contract, order, status, [], [])
        self._ib.wrapper.trades[(_CLIENT_ID, _ORDER_ID)] = self.trade
        return self.trade

    def sleep(self, _seconds):
        wrapper = self._ib.wrapper
        for step in self.__class__.steps:
            if step[0] == "error":
                _, req_id, code, msg = step
                wrapper.error(req_id, code, msg, "")
            else:
                _, status, perm_id = step
                qty = float(self.trade.order.totalQuantity)
                filled = qty if status == IBOrderStatus.Filled else 0.0
                wrapper.orderStatus(_ORDER_ID, status, filled, qty - filled, 0.0, perm_id, 0, 0.0, _CLIENT_ID, "", 0.0)

    def disconnect(self):
        return None


@pytest.fixture
def cli(monkeypatch):
    _WrapperClient.steps = []
    _WrapperClient.connected = False
    monkeypatch.setattr(ib_place_order, "IBClient", _WrapperClient)
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


# --- accepted orders ---------------------------------------------------------


def test_399_after_presubmitted_ack_is_ok(cli):
    # Wrapper.error turns the PreSubmitted trade into ValidationError; the
    # permId proves IB accepted the order.
    _WrapperClient.steps = [("status", "PreSubmitted", 123456), ("error", _ORDER_ID, 399, _W399)]

    result = cli.place_order(dict(_COMBO))

    assert result["status"] == "ok"
    assert result["orderId"] == _ORDER_ID
    assert result["permId"] == 123456
    assert result["initialStatus"] == "ValidationError"
    assert result["ibNotices"] == [{"reqId": _ORDER_ID, "code": 399, "message": _W399}]
    # The real placing clientId, so cancel / modify can reuse it.
    assert result["clientId"] == _CLIENT_ID


def test_399_before_presubmitted_ack_is_ok(cli):
    _WrapperClient.steps = [("error", _ORDER_ID, 399, _W399), ("status", "PreSubmitted", 123456)]

    result = cli.place_order(dict(_COMBO))

    assert result["status"] == "ok"
    assert result["initialStatus"] == "PreSubmitted"


def test_connection_wide_2110_req_id_minus_one_is_ok(cli):
    _WrapperClient.steps = [
        ("error", -1, 2110, "Connectivity between TWS and server is broken. It will be restored automatically."),
        ("status", "Submitted", 555),
    ]

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "ok"
    assert result["ibNotices"][0] == {"reqId": -1, "code": 2110, "message": result["ibNotices"][0]["message"]}


def test_other_req_id_hard_error_does_not_reject_this_order(cli):
    _WrapperClient.steps = [
        ("error", 99, 200, "No security definition has been found for the request"),
        ("status", "Submitted", 555),
    ]

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "ok"
    assert result["ibNotices"][0]["reqId"] == 99


def test_filled_immediately_is_ok(cli):
    _WrapperClient.steps = [("status", "Filled", 9)]

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "ok"
    assert result["permId"] == 9


# --- unconfirmed (no ack within the wait) -------------------------------------


def test_no_ack_and_no_error_is_unconfirmed_ok(cli):
    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "ok"
    assert result["initialStatus"] == "PendingSubmit"
    assert result["permId"] == 0


def test_connection_loss_before_ack_is_unconfirmed_ok(cli):
    _WrapperClient.steps = [("error", -1, 1100, "Connectivity between IB and Trader Workstation has been lost.")]

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "ok"
    assert result["ibNotices"][0]["code"] == 1100


# --- rejected orders ---------------------------------------------------------


def test_201_then_399_keeps_the_rejection(cli):
    # Wrapper.error: 201 → Cancelled, then 399 overwrites to ValidationError.
    _WrapperClient.steps = [
        ("error", _ORDER_ID, 201, "Order rejected - reason: insufficient buying power"),
        ("error", _ORDER_ID, 399, _W399),
    ]

    result = cli.place_order(dict(_STOCK))

    assert result["initialStatus"] == "ValidationError"
    assert result["status"] == "error"
    assert result["code"] == 201
    assert "insufficient buying power" in result["message"]


def test_201_with_cancelled_status_is_error(cli):
    _WrapperClient.steps = [("error", _ORDER_ID, 201, "Order rejected - reason: insufficient buying power")]

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "error"
    assert result["code"] == 201
    assert result["initialStatus"] == "Cancelled"


@pytest.mark.parametrize(
    ("code", "msg"),
    [
        (434, "The order size cannot be zero."),
        (321, "Error validating request.-'bN' : cause - invalid order"),
    ],
)
def test_validation_error_without_perm_id_is_error(cli, code, msg):
    _WrapperClient.steps = [("error", _ORDER_ID, code, msg)]

    result = cli.place_order(dict(_STOCK))

    assert result["initialStatus"] == "ValidationError"
    assert result["status"] == "error"
    assert result["code"] == code


def test_399_alone_without_ack_is_error(cli):
    # Caveat of the rule: a warning with no permId within the wait is a
    # validation failure, even though IB may still ack the order later.
    _WrapperClient.steps = [("error", _ORDER_ID, 399, _W399)]

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "error"
    assert result["code"] == 399


def test_terminal_inactive_without_error_event_is_error(cli):
    _WrapperClient.steps = [("status", "Inactive", 5)]

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "error"
    assert result["initialStatus"] == "Inactive"


def test_code_110_still_returns_code_for_limit_off_tick(cli):
    _WrapperClient.steps = [
        ("error", _ORDER_ID, 110, "The price does not conform to the minimum price variation for this contract.")
    ]

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "error"
    assert result["code"] == 110
    assert result["initialStatus"] == "Cancelled"


def test_code_435_still_maps_to_account_unresolved(cli):
    _WrapperClient.steps = [("error", _ORDER_ID, 435, "You must specify an account.")]

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "error"
    assert result["reason_code"] == "ACCOUNT_UNRESOLVED"


# --- quantity validation (before any IB connection) ---------------------------


@pytest.mark.parametrize("quantity", [0, -1, 1.5, "1.5"])
def test_invalid_quantity_is_refused_before_connecting(cli, quantity):
    result = cli.place_order({**_COMBO, "quantity": quantity})

    assert result["status"] == "error"
    assert result["reason_code"] == "INVALID_ORDER_BODY"
    assert _WrapperClient.connected is False
