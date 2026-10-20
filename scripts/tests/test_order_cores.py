"""PR-10 T2 — connected-client cores for cancel / modify / place.

The cores act on an already-connected client and return the structured reply:
no print, no exit, no watchdog, no connect/disconnect; their errorEvent
listener is removed in ``finally``; amend state is invocation-local. The CLI
adapters keep the subprocess JSON contract (covered by the existing suites).
Fakes only — no IB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from xenon.clients.ib_client import IBConnectionError
from xenon.execution import ib_order_manage, ib_place_order
from xenon.execution.ib_order_manage import cancel_order_on_client, modify_order_on_client


class _Event:
    """errorEvent stand-in that counts listeners."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self

    def emit(self, *args):
        for handler in list(self.handlers):
            handler(*args)


def _trade(order_id=10, perm_id=12345, status="Submitted", lmt_price=20.0, client_id=4):
    trade = MagicMock()
    trade.order.orderId = order_id
    trade.order.permId = perm_id
    trade.order.orderType = "LMT"
    trade.order.lmtPrice = lmt_price
    trade.order.totalQuantity = 5
    trade.order.outsideRth = False
    trade.order.clientId = client_id
    trade.order.account = "U0000031"
    trade.orderStatus.status = status
    trade.contract.secType = "STK"
    return trade


def _client(snapshots, client_id=4):
    client = MagicMock()
    client.get_open_orders.side_effect = list(snapshots)
    client.ib.client.clientId = client_id
    client.ib.errorEvent = _Event()
    return client


def _assert_pure(client, capsys):
    assert client.ib.errorEvent.handlers == []
    client.disconnect.assert_not_called()
    client.connect.assert_not_called()
    assert capsys.readouterr().out == ""


# --- cancel -------------------------------------------------------------------


def test_cancel_core_returns_reply_without_exit_or_disconnect(capsys):
    t = _trade()
    client = _client([[t], []])  # gone on the first refresh = ack

    result = cancel_order_on_client(client, 10, 12345, account="U0000031", client_id=4)

    assert result == {
        "status": "ok",
        "message": "Order cancelled (orderId=10)",
        "orderId": 10,
        "finalStatus": "Cancelled",
    }
    client.cancel_order.assert_called_once_with(t.order)
    _assert_pure(client, capsys)


def test_cancel_core_fatal_error_removes_listener(capsys):
    t = _trade()
    client = _client([[t], [t]])
    client.cancel_order.side_effect = lambda _o: client.ib.errorEvent.emit(10, 10147, "not found")

    result = cancel_order_on_client(client, 10, 12345)

    assert result["status"] == "error"
    assert result["upstream"]["code"] == 10147
    # A real IB reject code after the send: rejected, never unconfirmed.
    assert result["amend_outcome"] == "rejected"
    _assert_pure(client, capsys)


def test_cancel_core_without_ack_is_unconfirmed_not_reject(capsys):
    """F4 (Astra T1–T3): the cancel was sent and IB never acknowledged it
    (order still Submitted after the poll) — that is not an IB reject."""
    t = _trade()
    client = _client([[t]] * 12)

    result = cancel_order_on_client(client, 10, 12345)

    assert result["status"] == "error"
    assert (result["classification"], result["amend_outcome"]) == ("unconfirmed", "unconfirmed")
    client.cancel_order.assert_called_once_with(t.order)
    _assert_pure(client, capsys)


def test_cancel_core_refuses_foreign_owner_by_default(capsys):
    """Default ensure_owner never reconnects: another clientId's order → ownership."""
    t = _trade(client_id=26)
    client = _client([[t]], client_id=4)

    result = cancel_order_on_client(client, 10, 12345)

    assert result["status"] == "error"
    assert result["classification"] == "ownership"
    assert result["originalClientId"] == 26
    client.cancel_order.assert_not_called()
    _assert_pure(client, capsys)


def test_cancel_core_connection_drop_is_a_connection_reply(capsys):
    t = _trade()
    client = _client([[t]])
    client.cancel_order.side_effect = IBConnectionError("socket closed")

    result = cancel_order_on_client(client, 10, 12345)

    assert result["classification"] == "connection"
    assert result["message"].startswith("IB connection lost")
    _assert_pure(client, capsys)


def test_unexpected_exception_still_removes_listener():
    t = _trade()
    client = _client([[t]])
    client.cancel_order.side_effect = RuntimeError("bug")

    with pytest.raises(RuntimeError):
        cancel_order_on_client(client, 10, 12345)
    assert client.ib.errorEvent.handlers == []


# --- modify -------------------------------------------------------------------


def test_modify_core_confirmed_reply(capsys):
    t = _trade(lmt_price=20.0)
    confirmed = _trade(lmt_price=22.5)
    client = _client([[t], [confirmed]])

    result = modify_order_on_client(client, 10, 12345, 22.5)

    assert result["status"] == "ok"
    assert result["confirmedPrice"] == 22.5
    client.place_order.assert_called_once_with(t.contract, t.order)
    _assert_pure(client, capsys)


def test_modify_core_amend_stage_is_invocation_local(capsys):
    """Unconfirmed on one invocation never leaks into the next one."""
    t = _trade(lmt_price=20.0)
    stale = [_trade(lmt_price=20.0)] * 10
    client = _client([[t], *[[x] for x in stale]])
    first = modify_order_on_client(client, 10, 12345, 22.5)
    assert first["amend_outcome"] == "unconfirmed"

    # Second invocation on the SAME client fails before sending.
    client.get_open_orders.side_effect = [[_trade()]]
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    second = modify_order_on_client(client, 10, 12345, 22.5, send_by=past)
    assert second["classification"] == "send_window_closed"
    assert second["amend_outcome"] == "not_sent"
    assert client.place_order.call_count == 1  # only the first one sent
    _assert_pure(client, capsys)


def test_modify_core_rejected_reply_removes_listener(capsys):
    t = _trade()
    client = _client([[t], [_trade(lmt_price=20.0)]])  # broker still holds the old price
    client.place_order.side_effect = lambda *_a: client.ib.errorEvent.emit(10, 201, "rejected")

    result = modify_order_on_client(client, 10, 12345, 22.5)

    assert result["amend_outcome"] == "rejected"
    assert result["upstream"]["code"] == 201
    _assert_pure(client, capsys)


def test_cli_adapters_keep_the_reconnect_hook(monkeypatch):
    """The CLI adapter (subprocess) still re-identifies as the owner."""
    seen = []
    monkeypatch.setattr(
        ib_order_manage, "_reconnect_as_owner_core", lambda c, h, p, owner: seen.append((h, p, owner))
    )
    t = _trade(client_id=26)
    client = _client([[t], [t], []], client_id=20)
    with pytest.raises(SystemExit) as exc:
        ib_order_manage.cancel_order(client, 10, 12345, "127.0.0.1", 4002)
    assert exc.value.code == 0
    assert seen == [("127.0.0.1", 4002, 26)]


# --- place --------------------------------------------------------------------


class _PlaceClient:
    def __init__(self):
        self._ib = SimpleNamespace(errorEvent=_Event())
        self._last_client_id = 104
        self.connect = MagicMock()
        self.disconnect = MagicMock()

    def qualify_all(self, *contracts):
        for idx, contract in enumerate(contracts, start=1):
            contract.conId = idx
        return list(contracts)

    def qualify_one(self, contract):
        return self.qualify_all(contract)[0]

    def place_order(self, _contract, order):
        log = [SimpleNamespace(status="Submitted", message="", errorCode=0)]
        return SimpleNamespace(
            order=SimpleNamespace(orderId=7, permId=555, tif=order.tif, clientId=104),
            orderStatus=SimpleNamespace(status="Submitted", permId=555),
            log=log,
        )

    def sleep(self, _seconds):
        return None


_COMBO = {
    "type": "combo",
    "symbol": "SPY",
    "action": "BUY",
    "quantity": 1,
    "limitPrice": 0.5,
    "account": "U0000031",
    "legs": [
        {"expiry": "20261120", "strike": 500, "right": "C", "action": "BUY"},
        {"expiry": "20261120", "strike": 510, "right": "C", "action": "SELL"},
    ],
}


def test_place_core_is_pure_and_repeatable(capsys):
    client = _PlaceClient()
    for _ in range(3):
        result = ib_place_order.place_order_on_client(client, dict(_COMBO))
        assert result["status"] == "ok"
        assert result["outcome"] == "accepted"
        assert result["clientId"] == 104
    assert client._ib.errorEvent.handlers == []  # no listener accumulates
    client.connect.assert_not_called()
    client.disconnect.assert_not_called()
    assert capsys.readouterr().out == ""


def test_place_core_rejects_bad_quantity_without_touching_ib():
    client = _PlaceClient()
    client.qualify_one = MagicMock()
    result = ib_place_order.place_order_on_client(client, {**_COMBO, "quantity": 0})
    assert result["reason_code"] == "INVALID_ORDER_BODY"
    client.qualify_one.assert_not_called()


def test_place_core_send_exception_is_uncertain_and_cleans_up():
    client = _PlaceClient()

    def boom(*_a):
        raise OSError("socket closed mid-send")

    client.place_order = boom
    result = ib_place_order.place_order_on_client(client, dict(_COMBO))
    assert result["status"] == "uncertain"
    assert client._ib.errorEvent.handlers == []
