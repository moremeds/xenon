"""xenon-ib-place-order decides by broker acceptance, not by "any errorEvent".

Live incident 2026-10-17 (Saturday): a BUY 1 SPY BAG was accepted by IB
(PreSubmitted, permId set, visible in reqAllOpenOrders) but the CLI returned
"error" because IB sent a warning (399 is the expected cause) during the ack
wait. The place route then marked the reservation REJECTED while the order
was live at IB — orphan order, retry → duplicate order.

Rule under test (``ib_place_order.decide_outcome`` over the ordered
``Trade.log``; the final ``orderStatus.status`` is never trusted because
``Wrapper.error`` overwrites it on every warning). Round-3 summary:
- accepted := permId > 0 and status not Cancelled / ApiCancelled / Inactive
  (Filled counts) → ok.
- not accepted + an order-scoped non-notice error → error (earliest one).
- not accepted + a new-order validation failure (321 / 434) → error, even
  though ib_async classes them as warnings.
- not accepted + terminal status → error.
- otherwise (no ack yet: PendingSubmit / ApiPending, or ValidationError
  caused only by notices such as 399) → unconfirmed ok.

The client fake drives the REAL installed ib_async ``Wrapper.error`` and
``Wrapper.orderStatus`` against a registered ``Trade`` (the same registration
``IB.placeOrder`` does), so the status transitions — 201 → Cancelled, then a
later 399 → ValidationError — are ib_async's own. No network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from ib_async import IB, Execution, Trade
from ib_async import OrderStatus as IBOrderStatus

from xenon.execution import ib_place_order

_ORDER_ID = 7
_CLIENT_ID = 26


class _WrapperClient:
    """``IBClient`` stand-in backed by a real, unconnected ``ib_async.IB``.

    ``steps`` replay during the ack wait (``sleep``):
    ``("error", reqId, code, msg)`` → ``Wrapper.error``;
    ``("status", status, permId)`` → ``Wrapper.orderStatus``;
    ``("fill", permId, shares, price)`` → ``Wrapper.execDetails`` (live fill).
    """

    steps: list[tuple] = []

    last: "_WrapperClient | None" = None

    def __init__(self):
        self.__class__.last = self
        self._ib = IB()
        self._ib.wrapper.clientId = _CLIENT_ID
        self.trade: Trade | None = None

    def qualify_all(self, *contracts):
        for idx, contract in enumerate(contracts, start=1):
            contract.conId = idx
        return list(contracts)

    def qualify_one(self, contract):
        return self.qualify_all(contract)[0]

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
            elif step[0] == "fill":
                _, perm_id, shares, price = step
                execution = Execution(
                    execId=f"exec-{len(self.trade.fills) + 1}",
                    orderId=_ORDER_ID,
                    clientId=_CLIENT_ID,
                    permId=perm_id,
                    shares=shares,
                    price=price,
                )
                wrapper.execDetails(-1, self.trade.contract, execution)
            else:
                _, status, perm_id = step
                qty = float(self.trade.order.totalQuantity)
                filled = qty if status == IBOrderStatus.Filled else 0.0
                wrapper.orderStatus(_ORDER_ID, status, filled, qty - filled, 0.0, perm_id, 0, 0.0, _CLIENT_ID, "", 0.0)

    def disconnect(self):
        return None


@pytest.fixture
def cli():
    """``place_order(params)`` = the place core on a fresh connected stand-in."""
    _WrapperClient.steps = []
    _WrapperClient.last = None
    return SimpleNamespace(place_order=lambda params: ib_place_order.place_order_on_client(_WrapperClient(), params))


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
    assert result["outcome"] == "accepted"
    assert result["brokerStatus"] == "PreSubmitted"


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


def test_399_on_pending_submit_without_perm_id_is_unconfirmed_ok(cli):
    # Off-hours IB holds the order and may send the permId after the wait.
    # Wrapper.error relabels the PendingSubmit trade ValidationError; that is
    # a notice, not a rejection.
    _WrapperClient.steps = [("error", _ORDER_ID, 399, _W399)]

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "ok"
    assert result["orderId"] == _ORDER_ID
    assert result["permId"] == 0
    assert result["initialStatus"] == "ValidationError"
    assert result["ibNotices"] == [{"reqId": _ORDER_ID, "code": 399, "message": _W399}]


def test_399_then_434_without_perm_id_is_error(cli):
    _WrapperClient.steps = [
        ("error", _ORDER_ID, 399, _W399),
        ("error", _ORDER_ID, 434, "The order size cannot be zero."),
    ]

    result = cli.place_order(dict(_STOCK))

    assert result["status"] == "error"
    assert result["code"] == 434


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
def test_invalid_quantity_is_refused_before_place(cli, quantity):
    result = cli.place_order({**_COMBO, "quantity": quantity})

    assert result["status"] == "error"
    assert result["reason_code"] == "INVALID_ORDER_BODY"
    assert _WrapperClient.last.trade is None


# --- round 4: decide from the ordered Trade.log, never the final status ------


def test_presubmitted_then_201_then_399_is_rejected(cli):
    # A historical permId never clears a later rejection.
    _WrapperClient.steps = [
        ("status", "PreSubmitted", 123456),
        ("error", _ORDER_ID, 201, "Order rejected - reason: margin"),
        ("error", _ORDER_ID, 399, _W399),
    ]

    result = cli.place_order(dict(_STOCK))

    assert result["initialStatus"] == "ValidationError"
    assert result["permId"] == 123456
    assert result["outcome"] == "rejected"
    assert result["status"] == "error"
    assert result["code"] == 201
    assert result["message"] == "IB error 201: Order rejected - reason: margin"
    # PR-10 T4: an explicit IB rejection stays terminal on the order owner.
    assert result["amend_outcome"] == "rejected"


@pytest.mark.parametrize("perm_id", [0, 55])
def test_inactive_then_399_is_rejected(cli, perm_id):
    _WrapperClient.steps = [("status", "Inactive", perm_id), ("error", _ORDER_ID, 399, _W399)]

    result = cli.place_order(dict(_STOCK))

    assert result["initialStatus"] == "ValidationError"
    assert result["brokerStatus"] == "Inactive"
    assert result["outcome"] == "rejected"
    assert result["status"] == "error"


def test_presubmitted_399_submitted_is_accepted(cli):
    _WrapperClient.steps = [
        ("status", "PreSubmitted", 77),
        ("error", _ORDER_ID, 399, _W399),
        ("status", "Submitted", 77),
    ]

    result = cli.place_order(dict(_STOCK))

    assert result["outcome"] == "accepted"
    assert result["status"] == "ok"
    assert result["initialStatus"] == "Submitted"


def test_201_then_399_outcome_is_rejected(cli):
    _WrapperClient.steps = [("error", _ORDER_ID, 201, "Order rejected"), ("error", _ORDER_ID, 399, _W399)]

    assert cli.place_order(dict(_STOCK))["outcome"] == "rejected"


def test_399_only_outcome_is_unconfirmed(cli):
    _WrapperClient.steps = [("error", _ORDER_ID, 399, _W399)]

    result = cli.place_order(dict(_STOCK))

    assert result["outcome"] == "unconfirmed"
    assert result["status"] == "ok"


def test_filled_then_399_is_accepted(cli):
    _WrapperClient.steps = [("status", "Filled", 9), ("error", _ORDER_ID, 399, _W399)]

    result = cli.place_order(dict(_STOCK))

    assert result["initialStatus"] == "ValidationError"
    assert result["outcome"] == "accepted"
    assert result["status"] == "ok"


def test_hard_error_then_genuine_submitted_is_accepted(cli):
    # Any non-warning order error makes ib_async Wrapper.error cancel the
    # trade locally (Cancelled + errorCode log entry). A LATER genuine IB
    # orderStatus (errorCode 0) with a permId is IB's own word that the order
    # is working; rejecting it would orphan a live order and invite a
    # duplicate, so the later status clears the earlier hard error.
    _WrapperClient.steps = [
        ("error", _ORDER_ID, 10349, "order-scoped error"),
        ("status", "Submitted", 4242),
    ]

    result = cli.place_order(dict(_STOCK))

    assert result["outcome"] == "accepted"
    assert result["status"] == "ok"
    assert result["brokerStatus"] == "Submitted"
    assert result["permId"] == 4242


# --- round 5: a fill is acceptance; fill log entries are not status ----------


def test_presubmitted_201_399_then_fill_is_accepted(cli):
    _WrapperClient.steps = [
        ("status", "PreSubmitted", 55),
        ("error", _ORDER_ID, 201, "Order rejected - reason: margin"),
        ("error", _ORDER_ID, 399, _W399),
        ("fill", 55, 1.0, 1.0),
    ]

    result = cli.place_order(dict(_STOCK))

    assert _WrapperClient.last.trade.log[-1].message == "Fill 1.0@1.0"
    assert result["outcome"] == "accepted"
    assert result["status"] == "ok"
    assert result["filled"] is True
    assert result["fillCount"] == 1
    # The fill entry copied ValidationError; it is not broker status.
    assert result["brokerStatus"] == "PreSubmitted"


def test_inactive_399_then_fill_is_accepted(cli):
    _WrapperClient.steps = [
        ("status", "Inactive", 0),
        ("error", _ORDER_ID, 399, _W399),
        ("fill", 0, 1.0, 1.0),
    ]

    result = cli.place_order(dict(_STOCK))

    assert result["outcome"] == "accepted"
    assert result["filled"] is True
    assert result["brokerStatus"] == "Inactive"


def test_inactive_55_then_399_without_fill_is_rejected(cli):
    _WrapperClient.steps = [("status", "Inactive", 55), ("error", _ORDER_ID, 399, _W399)]

    result = cli.place_order(dict(_STOCK))

    assert result["outcome"] == "rejected"
    assert result["filled"] is False
    assert result["fillCount"] == 0


def test_201_399_without_fill_is_rejected(cli):
    _WrapperClient.steps = [("error", _ORDER_ID, 201, "Order rejected"), ("error", _ORDER_ID, 399, _W399)]

    result = cli.place_order(dict(_STOCK))

    assert result["outcome"] == "rejected"
    assert result["filled"] is False
