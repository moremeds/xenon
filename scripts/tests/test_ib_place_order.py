from types import SimpleNamespace

from xenon.execution import ib_place_order


class _ErrorEvent:
    def __init__(self):
        self._handler = None

    def __iadd__(self, handler):
        self._handler = handler
        return self

    def fire(self, req_id, code, msg):
        if self._handler is not None:
            self._handler(req_id, code, msg)


class _FakeIB:
    def __init__(self):
        self.errorEvent = _ErrorEvent()


class _FakeClient:
    placed_order = None
    placed_contract = None
    _error_to_fire: tuple | None = None  # (req_id, code, msg) to emit after place_order

    def __init__(self):
        self._ib = _FakeIB()

    def connect(self, **_kwargs):
        return None

    def qualify_contracts(self, *contracts):
        for idx, contract in enumerate(contracts, start=1):
            contract.conId = idx
        return list(contracts)

    def place_order(self, contract, order):
        self.__class__.placed_contract = contract
        self.__class__.placed_order = order
        return SimpleNamespace(
            order=SimpleNamespace(orderId=12345, permId=67890, tif=order.tif),
            orderStatus=SimpleNamespace(status="Submitted"),
        )

    def sleep(self, _seconds):
        if self.__class__._error_to_fire is not None:
            req_id, code, msg = self.__class__._error_to_fire
            self._ib.errorEvent.fire(req_id, code, msg)
            self.__class__._error_to_fire = None

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


def test_place_order_threads_outside_rth_to_ib_order(monkeypatch):
    monkeypatch.setattr(ib_place_order, "IBClient", _FakeClient)

    result = ib_place_order.place_order(
        {
            "type": "stock",
            "symbol": "QQQ",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 1.0,
            "outsideRth": True,
            "tif": "DAY",
        }
    )

    assert result["status"] == "ok"
    assert _FakeClient.placed_order.outsideRth is True


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


def test_place_order_uses_con_id_contract_for_subprocess_payload(monkeypatch):
    monkeypatch.setattr(ib_place_order, "IBClient", _FakeClient)

    result = ib_place_order.place_order(
        {
            "conId": 987654321,
            "secType": "OPT",
            "symbol": "SPX",
            "action": "SELL",
            "quantity": 1,
            "orderType": "MKT",
            "tif": "DAY",
            "orderRef": "xenon-pr-42",
        }
    )

    assert result["status"] == "ok"
    assert _FakeClient.placed_contract.conId == 987654321
    assert _FakeClient.placed_contract.secType == "OPT"
    assert _FakeClient.placed_order.orderRef == "xenon-pr-42"


def test_place_order_treats_ib_error_399_as_advisory_not_rejection(monkeypatch):
    """Regression: IB error 399 ('Order Message: order will not be placed until
    pre-market opens') is an advisory timing notice — the order IS accepted by IB
    and queued. It must not cause place_order to return status='error', which would
    trigger a 502 from the FastAPI route and prevent the arm_hook from receiving
    the fill event for outsideRth orders placed between ~20:00 and 04:00 ET.
    """
    _FakeClient._error_to_fire = (
        -1,
        399,
        "Order Message: BUY 100 SPY ARCA Warning: Your order will not be placed at the exchange until 2026-05-07 04:00:00 US/Eastern.",
    )
    monkeypatch.setattr(ib_place_order, "IBClient", _FakeClient)

    result = ib_place_order.place_order(
        {
            "type": "stock",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 100,
            "limitPrice": 734.0,
            "outsideRth": True,
            "tif": "DAY",
        }
    )

    assert result["status"] == "ok", (
        f"Error 399 must be treated as advisory; got status={result['status']!r} message={result.get('message')!r}"
    )
    assert result["permId"] == 67890


def test_place_order_treats_ib_error_2109_as_advisory_not_rejection(monkeypatch):
    """Regression: IB error 2109 ('outsideRth ignored for MKT/SMART, order being processed')
    is advisory — the order is placed. Treating it as fatal causes a 502 and prevents
    the fill event from reaching arm_hook.
    """
    _FakeClient._error_to_fire = (
        -1,
        2109,
        "Order Event Warning:Attribute 'Outside Regular Trading Hours' is ignored based on the order type and destination. PlaceOrder is now being processed.",
    )
    monkeypatch.setattr(ib_place_order, "IBClient", _FakeClient)

    result = ib_place_order.place_order(
        {
            "type": "stock",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 100,
            "orderType": "MKT",
            "outsideRth": True,
            "tif": "DAY",
        }
    )

    assert result["status"] == "ok", f"Error 2109 must be advisory; got {result['status']!r}"
    assert result["permId"] == 67890
