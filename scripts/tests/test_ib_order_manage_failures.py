"""F5.1 — Failure classification in ib_order_manage.py.

Tests that the subprocess emits `classification` in its JSON output,
distinguishing connection / ownership / ib_reject failures per SL §8.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

# Add scripts dir to path (same pattern as sibling test_ib_order_manage.py)
sys.path.insert(0, str(Path(__file__).parent.parent))

from xenon.execution import ib_order_manage
from xenon.execution.ib_order_manage import (
    cancel_order,
    classify_failure,
    modify_order,
)

# ─── Helpers (mirror sibling test) ───────────────────────


def make_trade(order_id=10, perm_id=12345, status="Submitted", order_type="LMT", lmt_price=22.50, client_id=0):
    trade = MagicMock()
    trade.order.orderId = order_id
    trade.order.permId = perm_id
    trade.order.orderType = order_type
    trade.order.lmtPrice = lmt_price
    trade.order.totalQuantity = 50
    trade.order.outsideRth = False
    trade.order.clientId = client_id
    trade.orderStatus.status = status
    trade.contract = MagicMock()
    trade.contract.secType = "OPT"
    return trade


def make_client(trades=None):
    client = MagicMock()
    client.get_open_orders.return_value = trades or []
    client.sleep = MagicMock()
    client.ib = MagicMock()
    client.ib.client.clientId = 0
    return client


# ─── classify_failure() pure helper ──────────────────────


class TestClassifyFailure:
    def test_connection_error_is_connection(self):
        assert classify_failure(None, ConnectionError("boom")) == "connection"

    def test_oserror_is_connection(self):
        assert classify_failure(None, OSError("handshake")) == "connection"

    def test_timeout_is_connection(self):
        assert classify_failure(None, TimeoutError("no socket")) == "connection"

    def test_326_is_ownership(self):
        assert classify_failure(326, None) == "ownership"

    def test_201_is_ib_reject(self):
        assert classify_failure(201, None) == "ib_reject"

    def test_10147_is_ib_reject(self):
        # Order-not-found is semantic, not connectivity — SL §8.
        assert classify_failure(10147, None) == "ib_reject"

    def test_103_is_ib_reject(self):
        assert classify_failure(103, None) == "ib_reject"

    def test_unknown_defaults_to_connection(self):
        assert classify_failure(None, None) == "connection"


# ─── cancel_order — classification emitted in output ─────


class TestCancelClassification:
    def test_classifies_ib_semantic_reject(self, capsys):
        """Error 201 (rejected) during cancel -> classification=ib_reject + upstream detail."""
        t = make_trade(status="Submitted", client_id=0)
        client = make_client([t])
        client.ib.client.clientId = 0

        captured_handler = {}

        class ErrorEvent:
            def __iadd__(self, handler):
                captured_handler["fn"] = handler
                return self

            def __isub__(self, handler):
                return self

        client.ib.errorEvent = ErrorEvent()

        def cancel_side_effect(order):
            # Simulate IB firing Error 201 on cancel
            captured_handler["fn"](t.order.orderId, 201, "Order rejected - reason test")

        client.cancel_order = MagicMock(side_effect=cancel_side_effect)

        with pytest.raises(SystemExit) as exc:
            cancel_order(client, 10, 12345, "127.0.0.1", 4001)
        assert exc.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "error"
        assert data["classification"] == "ib_reject"
        assert data["upstream"]["code"] == 201
        assert "rejected" in data["upstream"]["message"].lower()

    def test_classifies_ib_reject_10147(self, capsys):
        """Error 10147 (order not found) is semantic reject, not connection."""
        t = make_trade(status="Submitted", client_id=0)
        client = make_client([t])
        client.ib.client.clientId = 0

        captured_handler = {}

        class ErrorEvent:
            def __iadd__(self, handler):
                captured_handler["fn"] = handler
                return self

            def __isub__(self, handler):
                return self

        client.ib.errorEvent = ErrorEvent()

        def cancel_side_effect(order):
            captured_handler["fn"](t.order.orderId, 10147, "OrderId not found")

        client.cancel_order = MagicMock(side_effect=cancel_side_effect)

        with pytest.raises(SystemExit) as exc:
            cancel_order(client, 10, 12345, "127.0.0.1", 4001)
        assert exc.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["classification"] == "ib_reject"
        assert data["upstream"]["code"] == 10147


# ─── modify_order — classification emitted in output ─────


class TestModifyClassification:
    def test_modify_classifies_ib_reject(self, capsys):
        """Error 201 during modify -> classification=ib_reject."""
        t = make_trade(status="Submitted", order_type="LMT", lmt_price=20.00, client_id=0)
        # Refreshed trade keeps old price — simulates IB not applying the modify.
        stale = make_trade(status="Submitted", order_type="LMT", lmt_price=20.00, client_id=0)
        client = make_client()
        client.ib.client.clientId = 0
        client.get_open_orders.side_effect = [[t]] + [[stale]] * 10

        captured_handler = {}

        class ErrorEvent:
            def __iadd__(self, handler):
                captured_handler["fn"] = handler
                return self

            def __isub__(self, handler):
                return self

        client.ib.errorEvent = ErrorEvent()

        def place_side_effect(contract, order):
            captured_handler["fn"](t.order.orderId, 201, "Order rejected by exchange")

        client.place_order = MagicMock(side_effect=place_side_effect)

        with pytest.raises(SystemExit) as exc:
            modify_order(client, 10, 12345, 22.50, "127.0.0.1", 4001)
        assert exc.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["classification"] == "ib_reject"
        assert data["upstream"]["code"] == 201


# ─── main() — connection + ownership classification ──────


class TestMainConnectionClassification:
    def test_classifies_connection_error(self, capsys, monkeypatch):
        """Initial connect raises ConnectionError -> classification=connection."""
        fake_client = MagicMock()
        fake_client.connect.side_effect = ConnectionError("gateway down")

        monkeypatch.setattr(ib_order_manage, "IBClient", lambda: fake_client)
        monkeypatch.setattr(
            sys,
            "argv",
            ["ib_order_manage", "cancel", "--order-id", "10", "--perm-id", "12345"],
        )

        with pytest.raises(SystemExit) as exc:
            ib_order_manage.main()
        assert exc.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "error"
        assert data["classification"] == "connection"
        assert "gateway down" in data["message"]

    def test_classifies_clientid_in_use(self, capsys, monkeypatch):
        """When reconnecting as original_client_id hits 326 three times, classify ownership."""
        t = make_trade(status="Submitted", client_id=9)

        fake_client = make_client([t])
        fake_client.ib.client.clientId = 0  # Connected as 0, need to reconnect as 9

        # First connect (auto) succeeds. Subsequent reconnects (client_id=9) raise 326.
        def connect_side_effect(*args, **kwargs):
            cid = kwargs.get("client_id")
            if cid == 9:
                # IB 326 surfaces as RuntimeError (IBClient wraps string errors)
                raise RuntimeError("TWS API Error 326: client id is already in use")
            # initial auto connect
            return None

        fake_client.connect.side_effect = connect_side_effect
        fake_client.disconnect = MagicMock()

        monkeypatch.setattr(ib_order_manage, "IBClient", lambda: fake_client)
        monkeypatch.setattr(ib_order_manage.time, "sleep", lambda *_a, **_k: None)
        monkeypatch.setattr(
            sys,
            "argv",
            ["ib_order_manage", "cancel", "--order-id", "10", "--perm-id", "12345"],
        )

        with pytest.raises(SystemExit) as exc:
            ib_order_manage.main()
        assert exc.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["classification"] == "ownership"

        # 3 retries, always with the SAME original_client_id
        reconnect_calls = [c for c in fake_client.connect.call_args_list if c.kwargs.get("client_id") == 9]
        assert len(reconnect_calls) == 3, f"expected 3 retries with client_id=9, got {len(reconnect_calls)}"

    def test_clientid_in_use_sleep_count_exactly_two(self, capsys, monkeypatch):
        """Three 326 failures -> sleep called exactly twice (between retries) at 0.5s."""
        t = make_trade(status="Submitted", client_id=9)
        fake_client = make_client([t])
        fake_client.ib.client.clientId = 0

        def connect_side_effect(*args, **kwargs):
            if kwargs.get("client_id") == 9:
                raise RuntimeError("TWS API Error 326: client id is already in use")
            return None

        fake_client.connect.side_effect = connect_side_effect
        fake_client.disconnect = MagicMock()

        sleep_mock = MagicMock()
        monkeypatch.setattr(ib_order_manage.time, "sleep", sleep_mock)
        monkeypatch.setattr(ib_order_manage, "IBClient", lambda: fake_client)
        monkeypatch.setattr(
            sys,
            "argv",
            ["ib_order_manage", "cancel", "--order-id", "10", "--perm-id", "12345"],
        )

        with pytest.raises(SystemExit):
            ib_order_manage.main()

        # First retry failed -> sleep; second retry failed -> sleep; third -> no sleep.
        assert sleep_mock.call_count == 2
        assert sleep_mock.call_args_list == [call(0.5), call(0.5)]

    def test_clientid_in_use_first_retry_succeeds(self, capsys, monkeypatch):
        """326 on first reconnect, success on second -> cancel completes, one sleep."""
        t = make_trade(status="Submitted", client_id=9)

        # After reconnect, find_trade should still return the trade with clientId=9.
        # We then need cancel to proceed — simulate successful cancel via refreshed-trade=None.
        fake_client = make_client([t])
        fake_client.ib.client.clientId = 0  # mismatch forces reconnect

        # get_open_orders sequence:
        # 1) cancel_order initial find_trade -> [t]
        # 2) after reconnect find_trade -> [t]
        # 3+) refreshed lookups inside cancel loop -> [] (trade gone = cancelled)
        fake_client.get_open_orders.side_effect = [[t], [t]] + [[]] * 10

        connect_calls = {"n": 0}

        def connect_side_effect(*args, **kwargs):
            cid = kwargs.get("client_id")
            if cid == 9:
                connect_calls["n"] += 1
                if connect_calls["n"] == 1:
                    raise RuntimeError("TWS API Error 326: client id is already in use")
                # After successful reconnect, emulate ib.client.clientId flipping.
                fake_client.ib.client.clientId = 9
                return None
            return None

        fake_client.connect.side_effect = connect_side_effect
        fake_client.disconnect = MagicMock()

        # errorEvent plumbing
        class ErrorEvent:
            def __iadd__(self, handler):
                return self

            def __isub__(self, handler):
                return self

        fake_client.ib.errorEvent = ErrorEvent()
        fake_client.cancel_order = MagicMock()
        fake_client.sleep = MagicMock()

        sleep_mock = MagicMock()
        monkeypatch.setattr(ib_order_manage.time, "sleep", sleep_mock)
        monkeypatch.setattr(ib_order_manage, "IBClient", lambda: fake_client)
        monkeypatch.setattr(
            sys,
            "argv",
            ["ib_order_manage", "cancel", "--order-id", "10", "--perm-id", "12345"],
        )

        with pytest.raises(SystemExit) as exc:
            ib_order_manage.main()
        assert exc.value.code == 0
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "ok"
        # Exactly one backoff sleep between the two reconnect attempts.
        assert sleep_mock.call_count == 1
        assert sleep_mock.call_args_list == [call(0.5)]

    def test_trade_not_found_after_reconnect(self, capsys, monkeypatch):
        """Reconnect succeeds, but post-reconnect find_trade returns None -> ib_reject."""
        t = make_trade(status="Submitted", client_id=9)
        fake_client = make_client()
        fake_client.ib.client.clientId = 0

        # 1) initial find_trade -> [t]; 2) post-reconnect find_trade -> []
        fake_client.get_open_orders.side_effect = [[t], []]

        def connect_side_effect(*args, **kwargs):
            if kwargs.get("client_id") == 9:
                fake_client.ib.client.clientId = 9
            return None

        fake_client.connect.side_effect = connect_side_effect
        fake_client.disconnect = MagicMock()

        class ErrorEvent:
            def __iadd__(self, handler):
                return self

            def __isub__(self, handler):
                return self

        fake_client.ib.errorEvent = ErrorEvent()

        monkeypatch.setattr(ib_order_manage.time, "sleep", lambda *_a, **_k: None)
        monkeypatch.setattr(ib_order_manage, "IBClient", lambda: fake_client)
        monkeypatch.setattr(
            sys,
            "argv",
            ["ib_order_manage", "cancel", "--order-id", "10", "--perm-id", "12345"],
        )

        with pytest.raises(SystemExit) as exc:
            ib_order_manage.main()
        assert exc.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["status"] == "error"
        # Current behavior: post-reconnect missing trade is classified as ib_reject
        # (the trade vanished on the broker side; semantic not connectivity).
        assert data["classification"] == "ib_reject"
        assert "not found after reconnect" in data["message"].lower()
