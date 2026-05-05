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

    def qualify_contracts(self, *contracts):
        for idx, contract in enumerate(contracts, start=1):
            contract.conId = idx
        return list(contracts)

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


def test_place_order_accepts_market_payload_without_limit_price(monkeypatch):
    monkeypatch.setattr(ib_place_order, "IBClient", _FakeClient)

    result = ib_place_order.place_order(
        {
            "type": "stock",
            "symbol": "QQQ",
            "action": "SELL",
            "quantity": 1,
            "orderType": "MKT",
            "tif": "DAY",
        }
    )

    assert result["status"] == "ok"
    assert _FakeClient.placed_order.orderType == "MKT"


def test_place_order_accepts_stop_payload_without_limit_price(monkeypatch):
    monkeypatch.setattr(ib_place_order, "IBClient", _FakeClient)

    result = ib_place_order.place_order(
        {
            "type": "stock",
            "symbol": "QQQ",
            "action": "SELL",
            "quantity": 1,
            "orderType": "STP",
            "stopPrice": 400.0,
            "tif": "GTC",
        }
    )

    assert result["status"] == "ok"
    assert _FakeClient.placed_order.orderType == "STP"
    assert _FakeClient.placed_order.auxPrice == 400.0
