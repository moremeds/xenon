"""Order lifecycle preserves a foreign order's native venue/currency.

Finding (Task 4.5, verify-first): both cancel and modify source the contract
from the live IB open-order Trade (`find_trade` → `client.get_open_orders()`),
never from stored SMART/USD fields:
  * cancel_order calls client.cancel_order(trade.order) — contract not used.
  * modify_order calls client.place_order(trade.contract, trade.order) —
    trade.contract is the IB-supplied contract, which carries currency/exchange.
So NO order_submissions schema change is needed (YAGNI). This test locks in the
modify invariant: a JPY order is re-submitted with its JPY/TSEJ contract, not a
rebuilt SMART/USD one. (Live cancel is exercised in gated Task 5.3.)
"""

from types import SimpleNamespace

import pytest
from ib_async import Stock

from xenon.execution import ib_order_manage


class _Event:
    def __iadd__(self, _fn):
        return self

    def __isub__(self, _fn):
        return self


class _FakeClient:
    def __init__(self, trade):
        self._trade = trade
        # clientId matches the order's placer → no reconnect path.
        self.ib = SimpleNamespace(client=SimpleNamespace(clientId=30), errorEvent=_Event())
        self.placed: list = []

    def get_open_orders(self):
        return [self._trade]

    def place_order(self, contract, order):
        self.placed.append(contract)

    def cancel_order(self, order):
        pass

    def sleep(self, _s):
        pass


def _jpy_trade():
    # Real contract: 5016 JX Advanced Metals, TSEJ/JPY (2026-06-22).
    contract = Stock("5016", "TSEJ", "JPY")
    order = SimpleNamespace(
        permId=1001,
        orderId=55,
        clientId=30,
        lmtPrice=5267.0,
        totalQuantity=100.0,
        orderType="LMT",
        volatility=0.0,
        volatilityType=0,
        outsideRth=False,
        smartComboRoutingParams=None,
    )
    return SimpleNamespace(
        order=order,
        orderStatus=SimpleNamespace(status="Submitted"),
        contract=contract,
    )


def test_modify_reuses_foreign_ib_contract_not_smart_usd():
    trade = _jpy_trade()
    client = _FakeClient(trade)

    # output() prints JSON + sys.exit on the success path.
    with pytest.raises(SystemExit):
        ib_order_manage.modify_order(
            client,
            order_id=55,
            perm_id=1001,
            new_price=1.0,  # absurdly low, would never fill
            host="127.0.0.1",
            port=4002,
        )

    assert client.placed, "modify must re-submit the order"
    submitted = client.placed[0]
    assert submitted.currency == "JPY"
    assert submitted.exchange == "TSEJ"
    assert submitted.symbol == "5016"
