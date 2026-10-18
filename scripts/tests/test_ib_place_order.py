from types import SimpleNamespace

from xenon.execution import ib_place_order


class _ErrorEvent:
    def __iadd__(self, _handler):
        return self


class _FakeIB:
    errorEvent = _ErrorEvent()


class _FakeClient:
    placed_order = None

    def __init__(self):
        self._ib = _FakeIB()

    def connect(self, **_kwargs):
        return None

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


def test_place_order_returns_accepted_tif(monkeypatch):
    monkeypatch.setattr(ib_place_order, "IBClient", _FakeClient)

    result = ib_place_order.place_order(
        {
            "type": "stock",
            "symbol": "QQQ",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 1.0,
            "tif": "GTC",
        }
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


def test_order_ref_is_xo_submission_id_single_and_bag(monkeypatch):
    """PR-07: every Xenon order carries orderRef "xo:<submission_id>"; a
    body-supplied orderRef (e.g. a spoofed "leg_wizard:" ref) is ignored."""
    monkeypatch.setattr(ib_place_order, "IBClient", _FakeClient)
    for body in (_STOCK, _COMBO):
        ib_place_order.place_order({**body, "submission_id": "sub-1", "orderRef": "leg_wizard:spoof"})
        assert _FakeClient.placed_order.orderRef == "xo:sub-1"


def test_crash_after_place_is_uncertain_not_error(monkeypatch):
    """A crash after placeOrder (IB may hold the order) must never read as a
    rejection: the route would release the reservation and invite a resend."""

    class _CrashAfterPlace(_FakeClient):
        def sleep(self, _seconds):
            raise ConnectionError("socket closed mid-ack")

    monkeypatch.setattr(ib_place_order, "IBClient", _CrashAfterPlace)
    result = ib_place_order.place_order({**_STOCK, "submission_id": "sub-2"})
    assert result["status"] == "uncertain"
    assert result["orderId"] == 12345


def test_exception_from_place_order_is_uncertain(monkeypatch):
    """The send boundary is BEFORE place_order: an exception it raises may
    come after the socket write, so it is uncertain, never error."""

    class _RaisesInPlace(_FakeClient):
        def place_order(self, _contract, _order):
            raise ConnectionError("socket reset")

    monkeypatch.setattr(ib_place_order, "IBClient", _RaisesInPlace)
    assert ib_place_order.place_order(dict(_STOCK))["status"] == "uncertain"


def test_real_sdk_exception_after_send_is_uncertain(monkeypatch):
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

    monkeypatch.setattr(ib_place_order, "IBClient", _RealSdkClient)
    result = ib_place_order.place_order({**_STOCK, "submission_id": "sub-3"})
    assert sent == [99]
    assert result["status"] == "uncertain"


def test_account_error_before_send_stays_error(monkeypatch):
    from xenon.clients.ib_client import IBOrderAccountError

    class _NoAccount(_FakeClient):
        def place_order(self, _contract, _order):
            raise IBOrderAccountError("ACCOUNT_UNRESOLVED", "no managed account")

    monkeypatch.setattr(ib_place_order, "IBClient", _NoAccount)
    assert ib_place_order.place_order(dict(_STOCK))["status"] == "error"


def test_unqualified_contract_is_error_before_place(monkeypatch):
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
    monkeypatch.setattr(ib_place_order, "IBClient", _NoneQualify)
    for body in (_STOCK, _COMBO):
        result = ib_place_order.place_order(dict(body))
        assert result["status"] == "error"
        assert "Could not qualify" in result["message"]
    assert _NoneQualify.placed_order is None
