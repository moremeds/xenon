"""New IB orders must carry the scope account and fail closed otherwise.

Live incident 2026-10-17: the shared gateway reported two managed accounts,
no code path set ``Order.account``, and IB rejected every Xenon-placed new
order with error 435 ("You must specify an account"). With one managed
account IB silently picks its default, so an order could land on an account
other than the Gate-4 scope. See docs/reference/order-path-incident-history.md.

Fake IB only — no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from ib_async import LimitOrder

from xenon.clients.ib_client import IBClient, IBOrderAccountError

pytestmark = pytest.mark.usefixtures("bypass_broker_boundary")


def _connected_client(MockIB, managed: list[str]) -> tuple[IBClient, MagicMock]:
    mock_ib = MockIB.return_value
    mock_ib.isConnected.return_value = True
    mock_ib.managedAccounts.return_value = list(managed)
    mock_ib.placeOrder.side_effect = lambda contract, order: SimpleNamespace(order=order, contract=contract)
    client = IBClient()
    client.connect(client_id=92)
    return client, mock_ib


def _new_order(account: str = "") -> LimitOrder:
    order = LimitOrder(action="BUY", totalQuantity=1, lmtPrice=1.0, tif="DAY")
    order.account = account
    return order


# ---------------------------------------------------------------------------
# IBClient.place_order — the single choke point for new orders
# ---------------------------------------------------------------------------


@patch("xenon.clients.ib_client.IB")
def test_two_managed_accounts_order_carries_scope_account(MockIB):
    client, mock_ib = _connected_client(MockIB, ["U1111111", "U2222222"])

    client.place_order(MagicMock(), _new_order("U2222222"))

    mock_ib.placeOrder.assert_called_once()
    _, placed = mock_ib.placeOrder.call_args.args
    assert placed.account == "U2222222"


@patch("xenon.clients.ib_client.IB")
def test_single_managed_account_still_places(MockIB):
    client, mock_ib = _connected_client(MockIB, ["DU0000000"])

    client.place_order(MagicMock(), _new_order("DU0000000"))

    _, placed = mock_ib.placeOrder.call_args.args
    assert placed.account == "DU0000000"


@patch("xenon.clients.ib_client.IB")
def test_scope_account_not_managed_fails_closed(MockIB):
    client, mock_ib = _connected_client(MockIB, ["U1111111", "U2222222"])

    with pytest.raises(IBOrderAccountError) as exc_info:
        client.place_order(MagicMock(), _new_order("U9999999"))

    assert exc_info.value.reason_code == "ACCOUNT_NOT_MANAGED"
    mock_ib.placeOrder.assert_not_called()


@pytest.mark.parametrize("account", ["", "   ", "legacy_unknown"])
@patch("xenon.clients.ib_client.IB")
def test_unresolved_account_fails_closed(MockIB, account):
    # Even with one managed account, never fall back to IB's default.
    client, mock_ib = _connected_client(MockIB, ["U1111111"])

    with pytest.raises(IBOrderAccountError) as exc_info:
        client.place_order(MagicMock(), _new_order(account))

    assert exc_info.value.reason_code == "ACCOUNT_UNRESOLVED"
    mock_ib.placeOrder.assert_not_called()


@patch("xenon.clients.ib_client.IB")
def test_modify_keeps_existing_order_account(MockIB):
    """Modify re-submits IB's own order object (orderId set) — leave it alone."""
    client, mock_ib = _connected_client(MockIB, ["U1111111", "U2222222"])
    order = _new_order("U2222222")
    order.orderId = 42

    # ib_order_manage's modify path calls place_order directly with trade.order.
    client.place_order(MagicMock(), order)

    mock_ib.placeOrder.assert_called_once()
    sent = mock_ib.placeOrder.call_args.args[1]
    assert sent.account == "U2222222"
    assert sent.orderId == 42


# ---------------------------------------------------------------------------
# xenon-ib-place-order CLI
# ---------------------------------------------------------------------------


class _ErrorEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):  # PR-10 T2: the core removes its listener
        self.handlers.remove(handler)
        return self


class _FakeCliClient:
    """Fake IBClient for ib_place_order: records the order, can emit IB errors."""

    placed_order = None
    raise_on_place: Exception | None = None
    emit_error: tuple[int, str] | None = None

    def __init__(self):
        self._ib = SimpleNamespace(errorEvent=_ErrorEvent())

    def connect(self, **_kwargs):
        return None

    def qualify_all(self, *contracts):
        for idx, contract in enumerate(contracts, start=1):
            contract.conId = idx
        return list(contracts)

    def qualify_one(self, contract):
        return self.qualify_all(contract)[0]

    def place_order(self, _contract, order):
        cls = self.__class__
        if cls.raise_on_place is not None:
            raise cls.raise_on_place
        cls.placed_order = order
        if cls.emit_error is not None:
            for handler in self._ib.errorEvent.handlers:
                handler(1, *cls.emit_error)  # reqId == the orderId placeOrder assigned
        # ib_async logs PendingSubmit on placeOrder and, on a hard error,
        # cancels the order with a log entry carrying the IB code.
        log = [SimpleNamespace(status="PendingSubmit", message="", errorCode=0)]
        if cls.emit_error is not None:
            code, msg = cls.emit_error
            log.append(SimpleNamespace(status="Cancelled", message=f"Error {code}, reqId 1: {msg}", errorCode=code))
        return SimpleNamespace(
            order=SimpleNamespace(orderId=1, permId=0, tif=order.tif),
            orderStatus=SimpleNamespace(status="Cancelled" if cls.emit_error else "PendingSubmit"),
            log=log,
        )

    def sleep(self, _seconds):
        return None

    def disconnect(self):
        return None


@pytest.fixture
def cli(monkeypatch):
    from xenon.execution import ib_place_order

    _FakeCliClient.placed_order = None
    _FakeCliClient.raise_on_place = None
    _FakeCliClient.emit_error = None
    monkeypatch.setattr(ib_place_order, "IBClient", _FakeCliClient)
    return ib_place_order


_STOCK = {"type": "stock", "symbol": "SPY", "action": "BUY", "quantity": 1, "limitPrice": 1.0}


def test_cli_stamps_payload_account_over_env(cli, monkeypatch):
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "U1111111")

    result = cli.place_order({**_STOCK, "account": "U2222222"})

    assert result["status"] == "ok"
    assert _FakeCliClient.placed_order.account == "U2222222"


def test_cli_falls_back_to_env_scope_account(cli, monkeypatch):
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "U1111111")

    cli.place_order(dict(_STOCK))

    assert _FakeCliClient.placed_order.account == "U1111111"


def test_cli_account_error_returns_reason_code(cli):
    _FakeCliClient.raise_on_place = IBOrderAccountError("ACCOUNT_NOT_MANAGED", "account 'U9' is not managed")

    result = cli.place_order({**_STOCK, "account": "U9"})

    assert result["status"] == "error"
    assert result["reason_code"] == "ACCOUNT_NOT_MANAGED"
    assert "not managed" in result["message"]


def test_cli_maps_ib_435_to_account_unresolved(cli):
    _FakeCliClient.emit_error = (435, "You must specify an account.")

    result = cli.place_order({**_STOCK, "account": "U2222222"})

    assert result["status"] == "error"
    assert result["code"] == 435
    assert result["reason_code"] == "ACCOUNT_UNRESOLVED"


# ---------------------------------------------------------------------------
# Other new-order callers
# ---------------------------------------------------------------------------


def test_combo_wizard_adapter_stamps_account(monkeypatch):
    from xenon.execution.combo_wizard import ib_adapter

    monkeypatch.setattr(ib_adapter, "_record_event", lambda *a, **k: None)
    monkeypatch.setattr(ib_adapter, "_wait_for_perm_id", lambda *_a: 7)
    ib = MagicMock()
    ib.qualify_contracts.return_value = [SimpleNamespace(conId=1), SimpleNamespace(conId=2)]
    adapter = ib_adapter.ComboWizardIbAdapter(ib, account="U2222222")
    legs = [
        {"symbol": "SPY", "expiry": "20261120", "strike": "500", "right": "C", "action": "BUY"},
        {"symbol": "SPY", "expiry": "20261120", "strike": "510", "right": "C", "action": "SELL"},
    ]

    adapter.place_combo_tp(session_id="s1", legs=legs, target_price=1, quantity=1)

    _, order = ib.place_order.call_args.args
    assert order.account == "U2222222"
