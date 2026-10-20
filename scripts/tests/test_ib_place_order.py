from types import SimpleNamespace

from xenon.execution import ib_place_order


class _ErrorEvent:
    def __iadd__(self, _handler):
        return self

    def __isub__(self, _handler):  # PR-10 T2: the core removes its listener
        return self


class _FakeIB:
    errorEvent = _ErrorEvent()


class _FakeClient:
    placed_order = None

    def __init__(self):
        self._ib = _FakeIB()

    def qualify_all(self, *contracts):
        for idx, contract in enumerate(contracts, start=1):
            contract.conId = idx
        return list(contracts)

    def qualify_one(self, contract):
        return self.qualify_all(contract)[0]

    def place_order(self, _contract, order):
        self.__class__.placed_order = order
        return SimpleNamespace(
            order=SimpleNamespace(orderId=12345, permId=67890, tif=order.tif),
            orderStatus=SimpleNamespace(status="Submitted"),
        )

    def sleep(self, _seconds):
        return None

    def disconnect(self):
        return None


def _place(client_cls, params):
    """The place core on a fresh connected stand-in (no CLI since PR-10)."""
    return ib_place_order.place_order_on_client(client_cls(), params)


def test_place_order_returns_accepted_tif():
    _client_cls = _FakeClient

    result = _place(
        _client_cls,
        {
            "type": "stock",
            "symbol": "QQQ",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 1.0,
            "tif": "GTC",
        },
    )

    assert _FakeClient.placed_order.tif == "GTC"
    assert result["tif"] == "GTC"


_STOCK = {"type": "stock", "symbol": "QQQ", "action": "BUY", "quantity": 1, "limitPrice": 1.0, "account": "DU0000000"}
_COMBO = {
    **_STOCK,
    "type": "combo",
    "legs": [
        {"expiry": "20261218", "strike": 500, "right": "C", "action": "BUY", "ratio": 1},
        {"expiry": "20261218", "strike": 510, "right": "C", "action": "SELL", "ratio": 1},
    ],
}


def test_order_ref_is_xo_submission_id_single_and_bag():
    """PR-07: every Xenon order carries orderRef "xo:<submission_id>"; a
    body-supplied orderRef (e.g. a spoofed "leg_wizard:" ref) is ignored."""
    _client_cls = _FakeClient
    for body in (_STOCK, _COMBO):
        _place(_client_cls, {**body, "submission_id": "sub-1", "orderRef": "leg_wizard:spoof"})
        assert _FakeClient.placed_order.orderRef == "xo:sub-1"


def test_crash_after_place_is_uncertain_not_error():
    """A crash after placeOrder (IB may hold the order) must never read as a
    rejection: the route would release the reservation and invite a resend."""

    class _CrashAfterPlace(_FakeClient):
        def sleep(self, _seconds):
            raise ConnectionError("socket closed mid-ack")

    _client_cls = _CrashAfterPlace
    result = _place(_client_cls, {**_STOCK, "submission_id": "sub-2"})
    assert result["status"] == "uncertain"
    assert result["orderId"] == 12345


def test_exception_from_place_order_is_uncertain():
    """The send boundary is BEFORE place_order: an exception it raises may
    come after the socket write, so it is uncertain, never error."""

    class _RaisesInPlace(_FakeClient):
        def place_order(self, _contract, _order):
            raise ConnectionError("socket reset")

    assert _place(_RaisesInPlace, dict(_STOCK))["status"] == "uncertain"


def test_real_sdk_exception_after_send_is_uncertain():
    """Real ib_async ``IB.placeOrder``: it sends (client.placeOrder) and only
    then does bookkeeping. An exception after the send must be uncertain."""
    from ib_async import IB

    from xenon.clients.ib_client import IBClient

    sent: list[int] = []

    class _RealSdkClient(_FakeClient):
        def __init__(self):
            self._ib = IB()
            self._ib.client.getReqId = lambda: 99
            self._ib.client.placeOrder = lambda order_id, _c, _o: sent.append(order_id)

            def _boom(*_a, **_k):
                raise RuntimeError("bookkeeping failed after send")

            self._ib.wrapper.orderKey = _boom
            self.logger = __import__("logging").getLogger("test")

        def place_order(self, contract, order):
            self._require_connection = lambda: None
            self._require_managed_account = lambda account: account
            return IBClient.place_order(self, contract, order)

    _client_cls = _RealSdkClient
    result = _place(_client_cls, {**_STOCK, "submission_id": "sub-3"})
    assert sent == [99]
    assert result["status"] == "uncertain"


def test_account_error_before_send_stays_error():
    from xenon.clients.ib_client import IBOrderAccountError

    class _NoAccount(_FakeClient):
        def place_order(self, _contract, _order):
            raise IBOrderAccountError("ACCOUNT_UNRESOLVED", "no managed account")

    result = _place(_NoAccount, dict(_STOCK))
    # Raised before ib.placeOrder: terminal for the order owner, never unconfirmed.
    assert (result["status"], result["amend_outcome"]) == ("error", "rejected")


def test_unqualified_contract_is_error_before_place():
    """ib_async 2.x returns [None] for an unknown contract; qualify_one raises
    IBContractError, so nothing is placed."""
    from xenon.clients.ib_client import IBClient

    class _NoneQualify(_FakeClient):
        def qualify_all(self, *contracts):
            fake = SimpleNamespace(qualifyContracts=lambda *c: [None] * len(c))
            probe = IBClient.__new__(IBClient)
            probe._ib = fake
            probe._require_connection = lambda: None
            return IBClient.qualify_all(probe, *contracts)

    _NoneQualify.placed_order = None
    _client_cls = _NoneQualify
    for body in (_STOCK, _COMBO):
        result = _place(_client_cls, dict(body))
        assert result["status"] == "error"
        assert "Could not qualify" in result["message"]
    assert _NoneQualify.placed_order is None


def test_owner_send_refusal_propagates_never_uncertain():
    """PR-10 T4: the owner's pre-send guard raises inside client.place_order
    (after the core's ``placed`` mark). It proves nothing was sent, so the
    core must re-raise it — never report ``uncertain``."""
    import pytest

    from xenon.clients.ib_client import SendRefused

    class _Refused(_FakeClient):
        def place_order(self, _contract, _order):
            raise SendRefused("broker readiness changed")

    with pytest.raises(SendRefused):
        _place(_Refused, dict(_STOCK))


def test_missing_fields_are_refused_before_place():
    _FakeClient.placed_order = None
    combo = {**_COMBO, "legs": [_COMBO["legs"][0]]}
    for body, missing in (
        ({k: v for k, v in _STOCK.items() if k != "limitPrice"}, "limitPrice"),
        ({**_STOCK, "type": "option"}, "expiry"),
        (combo, "legs (2+ entries)"),
    ):
        result = _place(_FakeClient, body)
        assert result["reason_code"] == "INVALID_ORDER_BODY" and missing in result["message"]
    assert _FakeClient.placed_order is None
