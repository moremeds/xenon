"""PR-05 — order identity tuple + IBConnectionError JSON contract.

``find_trade`` never falls back from a missing permId to orderId
(spec §2.4 / adv-7): orderIds collide across clientIds, and a stale or
missing permId means the order already changed identity. Any supplied
element of the identity tuple (scope account, owner clientId, orderId,
permId) that disagrees with the live trade rejects with an ownership-class
reason instead of guessing.

The module must emit the stdout JSON error contract — never a traceback —
when ``IBConnectionError`` (the real type from ``xenon.clients.ib_client``)
surfaces from connect, owner-reconnect, or mid-operation reads.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Add scripts dir to path (same pattern as sibling test_ib_order_manage.py)
sys.path.insert(0, str(Path(__file__).parent.parent))

from xenon.clients.ib_client import IBConnectionError
from xenon.execution import ib_order_manage
from xenon.execution.ib_order_manage import cancel_order, find_trade, modify_order


# ─── Helpers (mirror sibling tests, extended with account/clientId) ──────


def make_trade(
    order_id=10,
    perm_id=12345,
    status="Submitted",
    order_type="LMT",
    lmt_price=22.50,
    client_id=0,
    account="DU0000000",
):
    trade = MagicMock()
    trade.order.orderId = order_id
    trade.order.permId = perm_id
    trade.order.orderType = order_type
    trade.order.lmtPrice = lmt_price
    trade.order.totalQuantity = 50
    trade.order.outsideRth = False
    trade.order.clientId = client_id
    trade.order.account = account
    # Deterministic fallthrough: ib_async Trade has no .account attribute, so
    # _trade_account resolves order.account then trade.account — pin the
    # latter to None so a MagicMock can't satisfy the check by accident.
    trade.account = None
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


def _emitted(capsys):
    return json.loads(capsys.readouterr().out)


class ErrorEvent:
    """Minimal errorEvent stand-in for += / -= handler registration."""

    def __init__(self):
        self.handler = None

    def __iadd__(self, handler):
        self.handler = handler
        return self

    def __isub__(self, handler):
        return self


# ─── find_trade — strict permId identity ─────────────────────────────────


class TestFindTradeIdentity:
    def test_perm_id_present_never_falls_back_to_order_id(self):
        """permId asserted but absent → None even when orderId matches a
        different open order (could belong to another client/account)."""
        stranger = make_trade(order_id=10, perm_id=99999, client_id=9)
        client = make_client([stranger])
        assert find_trade(client, 10, 12345) is None

    def test_order_id_mismatch_on_perm_id_hit_rejects(self, capsys):
        """permId matched but the asserted orderId disagrees → identity
        reject (ownership), never return the trade."""
        t = make_trade(order_id=11, perm_id=12345)
        client = make_client([t])
        with pytest.raises(SystemExit) as exc:
            find_trade(client, 10, 12345)
        assert exc.value.code == 1
        data = _emitted(capsys)
        assert data["status"] == "error"
        assert data["classification"] == "ownership"
        assert "mismatch" in data["message"].lower()

    def test_duplicate_order_id_across_clients_is_ambiguous(self, capsys):
        """orderId-only request with two open orders sharing that orderId on
        different clients → reject; never silently pick the first."""
        a = make_trade(order_id=10, perm_id=111, client_id=9)
        b = make_trade(order_id=10, perm_id=222, client_id=26)
        client = make_client([a, b])
        with pytest.raises(SystemExit) as exc:
            find_trade(client, 10, 0)
        assert exc.value.code == 1
        data = _emitted(capsys)
        assert data["classification"] == "ownership"
        assert "ambiguous" in data["message"].lower() or "permId" in data["message"]

    def test_duplicate_order_id_disambiguated_by_owner_client_id(self):
        """(owner clientId, orderId) is a complete IB identity — the other
        client's order is never a candidate."""
        ours = make_trade(order_id=10, perm_id=111, client_id=9)
        theirs = make_trade(order_id=10, perm_id=222, client_id=26)
        client = make_client([ours, theirs])
        assert find_trade(client, 10, 0, client_id=9) is ours

    def test_account_mismatch_rejects(self, capsys):
        """Asserted scope account differs from the live order's account →
        reject (a cancel must never reach across accounts)."""
        t = make_trade(order_id=10, perm_id=12345, account="DU9999999")
        client = make_client([t])
        with pytest.raises(SystemExit) as exc:
            find_trade(client, 10, 12345, account="DU0000000")
        assert exc.value.code == 1
        data = _emitted(capsys)
        assert data["classification"] == "ownership"
        assert "account" in data["message"].lower()

    def test_owner_client_id_mismatch_rejects(self, capsys):
        """Caller-recorded placing clientId disagrees with the live order's
        owner clientId → reject."""
        t = make_trade(order_id=10, perm_id=12345, client_id=9)
        client = make_client([t])
        with pytest.raises(SystemExit) as exc:
            find_trade(client, 10, 12345, client_id=26)
        assert exc.value.code == 1
        data = _emitted(capsys)
        assert data["classification"] == "ownership"

    def test_full_tuple_match_returns_trade(self):
        t = make_trade(order_id=10, perm_id=12345, client_id=9, account="DU0000000")
        client = make_client([t])
        assert find_trade(client, 10, 12345, account="DU0000000", client_id=9) is t


# ─── Empty account field — unverified, not a mismatch ────────────────────


class TestAccountUnverified:
    def test_missing_trade_account_is_not_a_mismatch(self):
        """IB open-order snapshots do not always populate order.account —
        an empty/None field must not block the operation; only a present
        account that disagrees rejects."""
        t = make_trade(order_id=10, perm_id=12345, account=None)
        client = make_client([t])
        assert find_trade(client, 10, 12345, account="DU0000000") is t

    def test_empty_string_trade_account_is_not_a_mismatch(self):
        """ib_async defaults Order.account to '' — same treatment as None."""
        t = make_trade(order_id=10, perm_id=12345, account="")
        client = make_client([t])
        assert find_trade(client, 10, 12345, account="DU0000000") is t

    def test_both_accounts_present_and_differ_still_rejects(self, capsys):
        """The guard only loosens on a MISSING field — DU9999999 vs an
        asserted DU0000000 stays an ownership reject."""
        t = make_trade(order_id=10, perm_id=12345, account="DU9999999")
        client = make_client([t])
        with pytest.raises(SystemExit) as exc:
            find_trade(client, 10, 12345, account="DU0000000")
        assert exc.value.code == 1
        assert _emitted(capsys)["classification"] == "ownership"

    def test_cancel_ok_output_flags_account_unverified(self, capsys):
        """Cancel success on an account-less trade carries
        account_unverified=true in the JSON extra."""
        t = make_trade(order_id=10, perm_id=12345, account=None, client_id=0)
        client = make_client()
        client.ib.client.clientId = 0
        client.ib.errorEvent = ErrorEvent()
        client.get_open_orders.side_effect = [[t], []]
        client.cancel_order = MagicMock()
        with pytest.raises(SystemExit) as exc:
            cancel_order(client, 10, 12345, "127.0.0.1", 4002, account="DU0000000")
        assert exc.value.code == 0
        data = _emitted(capsys)
        assert data["status"] == "ok"
        assert data["account_unverified"] is True

    def test_cancel_err_output_flags_account_unverified(self, capsys):
        """A cancel timeout error on an account-less trade also carries the
        flag — err extras get the note too."""
        t = make_trade(order_id=10, perm_id=12345, account=None, client_id=0)
        client = make_client()
        client.ib.client.clientId = 0
        client.ib.errorEvent = ErrorEvent()
        client.get_open_orders.return_value = [t]  # never disappears → timeout
        client.cancel_order = MagicMock()
        with pytest.raises(SystemExit) as exc:
            cancel_order(client, 10, 12345, "127.0.0.1", 4002, account="DU0000000")
        assert exc.value.code == 1
        data = _emitted(capsys)
        assert data["status"] == "error"
        assert data["account_unverified"] is True

    def test_verified_account_omits_flag(self, capsys):
        """When the trade does carry the asserted account there is nothing
        unverified — no flag in the output."""
        t = make_trade(order_id=10, perm_id=12345, account="DU0000000", client_id=0)
        client = make_client()
        client.ib.client.clientId = 0
        client.ib.errorEvent = ErrorEvent()
        client.get_open_orders.side_effect = [[t], []]
        client.cancel_order = MagicMock()
        with pytest.raises(SystemExit) as exc:
            cancel_order(client, 10, 12345, "127.0.0.1", 4002, account="DU0000000")
        assert exc.value.code == 0
        assert "account_unverified" not in _emitted(capsys)


# ─── Signed BAG price — credit combos carry negative limits ──────────────


class TestSignedBagModifyPrice:
    def test_bag_modify_negative_price_accepted(self, capsys):
        """Credit combos carry a NEGATIVE BAG limit — modify must accept a
        signed price and send it through to place_order."""
        t = make_trade(order_id=10, perm_id=12345, client_id=0)
        t.contract.secType = "BAG"
        client = make_client()
        client.ib.client.clientId = 0
        client.ib.errorEvent = ErrorEvent()
        # The mutated order reports the new price on the first refresh →
        # confirmed immediately.
        client.get_open_orders.return_value = [t]
        client.place_order = MagicMock()
        with pytest.raises(SystemExit) as exc:
            modify_order(client, 10, 12345, -1.25, "127.0.0.1", 4002)
        assert exc.value.code == 0
        data = _emitted(capsys)
        assert data["status"] == "ok"
        placed = client.place_order.call_args[0][1]
        assert placed.lmtPrice == -1.25

    def test_single_leg_negative_price_rejected(self, capsys):
        """Negative limits are still illegal on non-BAG orders."""
        t = make_trade(order_id=10, perm_id=12345, client_id=0)
        client = make_client([t])
        client.ib.client.clientId = 0
        client.ib.errorEvent = ErrorEvent()
        client.place_order = MagicMock()
        with pytest.raises(SystemExit) as exc:
            modify_order(client, 10, 12345, -1.25, "127.0.0.1", 4002)
        assert exc.value.code == 1
        data = _emitted(capsys)
        assert data["status"] == "error"
        assert data["classification"] == "ib_reject"
        client.place_order.assert_not_called()

    def test_bag_modify_zero_price_rejected(self, capsys):
        """Zero is invalid even on a combo — a net-0 limit is meaningless."""
        t = make_trade(order_id=10, perm_id=12345, client_id=0)
        t.contract.secType = "BAG"
        client = make_client([t])
        client.ib.client.clientId = 0
        client.ib.errorEvent = ErrorEvent()
        client.place_order = MagicMock()
        with pytest.raises(SystemExit) as exc:
            modify_order(client, 10, 12345, 0, "127.0.0.1", 4002)
        assert exc.value.code == 1
        assert _emitted(capsys)["status"] == "error"
        client.place_order.assert_not_called()

    def test_single_leg_zero_price_rejected(self, capsys):
        t = make_trade(order_id=10, perm_id=12345, client_id=0)
        client = make_client([t])
        client.ib.client.clientId = 0
        client.ib.errorEvent = ErrorEvent()
        client.place_order = MagicMock()
        with pytest.raises(SystemExit) as exc:
            modify_order(client, 10, 12345, 0, "127.0.0.1", 4002)
        assert exc.value.code == 1
        client.place_order.assert_not_called()


# ─── cancel_order — identity enforcement end-to-end ──────────────────────


class TestCancelIdentity:
    def test_filled_target_never_touches_stranger_with_same_order_id(self, capsys):
        """Target permId already gone (filled before cancel); a stranger
        holds the same orderId → not-found error, stranger never cancelled."""
        stranger = make_trade(order_id=10, perm_id=777, client_id=9)
        client = make_client([stranger])
        with pytest.raises(SystemExit) as exc:
            cancel_order(client, 10, 12345, "127.0.0.1", 4002)
        assert exc.value.code == 1
        data = _emitted(capsys)
        assert data["upstream"]["code"] == 10147
        client.cancel_order.assert_not_called()

    def test_disappearance_success_only_counts_verified_identity(self, capsys):
        """After cancelling our permId-verified order, disappearance counts
        as success even though a stranger with the same orderId stays open."""
        ours = make_trade(order_id=10, perm_id=12345, client_id=0)
        stranger = make_trade(order_id=10, perm_id=777, client_id=9)
        client = make_client()
        client.ib.client.clientId = 0
        client.ib.errorEvent = ErrorEvent()
        # 1) initial find → both open; 2) first refresh → ours gone.
        client.get_open_orders.side_effect = [[ours, stranger], [stranger]]
        client.cancel_order = MagicMock()

        with pytest.raises(SystemExit) as exc:
            cancel_order(client, 10, 12345, "127.0.0.1", 4002)
        assert exc.value.code == 0
        data = _emitted(capsys)
        assert data["status"] == "ok"
        assert data["finalStatus"] == "Cancelled"
        client.cancel_order.assert_called_once_with(ours.order)

    def test_cancel_wrong_account_rejects_before_reconnect(self, capsys):
        t = make_trade(order_id=10, perm_id=12345, account="DU9999999", client_id=9)
        client = make_client([t])
        with pytest.raises(SystemExit) as exc:
            cancel_order(client, 10, 12345, "127.0.0.1", 4002, account="DU0000000")
        assert exc.value.code == 1
        client.disconnect.assert_not_called()
        client.cancel_order.assert_not_called()


# ─── modify_order — identity enforcement ─────────────────────────────────


class TestModifyIdentity:
    def test_modify_order_id_mismatch_rejects_before_place(self, capsys):
        t = make_trade(order_id=11, perm_id=12345)
        client = make_client([t])
        with pytest.raises(SystemExit) as exc:
            modify_order(client, 10, 12345, 22.50, "127.0.0.1", 4002)
        assert exc.value.code == 1
        data = _emitted(capsys)
        assert data["classification"] == "ownership"
        client.place_order.assert_not_called()

    def test_modify_wrong_account_rejects(self, capsys):
        t = make_trade(order_id=10, perm_id=12345, account="DU9999999")
        client = make_client([t])
        with pytest.raises(SystemExit) as exc:
            modify_order(client, 10, 12345, 22.50, "127.0.0.1", 4002, account="DU0000000")
        assert exc.value.code == 1
        client.place_order.assert_not_called()


# ─── IBConnectionError → JSON contract ───────────────────────────────────


class TestIBConnectionErrorContract:
    def test_classify_failure_real_type_is_connection(self):
        assert ib_order_manage.classify_failure(None, IBConnectionError("no socket")) == "connection"

    def test_classify_failure_ibconnectionerror_wrapping_326_is_ownership(self):
        """A 326 surfaced inside IBConnectionError must keep the ownership
        classification — check order matters."""
        exc = IBConnectionError(
            "Failed to connect to IB on 127.0.0.1:4002 after 1 attempt(s): "
            "Error 326 client id is already in use"
        )
        assert ib_order_manage.classify_failure(None, exc) == "ownership"

    def test_reconnect_as_owner_emits_json_on_ibconnectionerror(self, capsys):
        """IBConnectionError from owner-reconnect emits the JSON contract
        (classification=connection), not a crash/traceback."""
        client = make_client([])
        client.connect.side_effect = IBConnectionError("Failed to connect to IB on 127.0.0.1:4002")
        with pytest.raises(SystemExit) as exc:
            ib_order_manage._reconnect_as_owner(client, "127.0.0.1", 4002, 9)
        assert exc.value.code == 1
        data = _emitted(capsys)
        assert data["status"] == "error"
        assert data["classification"] == "connection"

    def test_main_connect_ibconnectionerror_emits_json(self, capsys, monkeypatch):
        """Initial connect raising the real IBConnectionError emits JSON."""
        fake = make_client()
        fake.connect.side_effect = IBConnectionError(
            "Failed to connect to IB on 127.0.0.1:4002: all client IDs 20-49 in use"
        )
        monkeypatch.setattr(ib_order_manage, "IBClient", lambda: fake)
        monkeypatch.setattr(sys, "argv", ["x", "cancel", "--order-id", "10", "--perm-id", "12345"])
        with pytest.raises(SystemExit) as exc:
            ib_order_manage.main()
        assert exc.value.code == 1
        data = _emitted(capsys)
        assert data["status"] == "error"
        assert data["classification"] == "connection"

    def test_mid_cancel_ibconnectionerror_emits_json(self, capsys, monkeypatch):
        """get_open_orders raising IBConnectionError mid-cancel must emit
        JSON instead of crashing with a traceback."""
        fake = make_client()
        fake.connect.return_value = None
        fake.get_open_orders.side_effect = IBConnectionError("Not connected to IB. Call connect() first.")
        monkeypatch.setattr(ib_order_manage, "IBClient", lambda: fake)
        monkeypatch.setattr(sys, "argv", ["x", "cancel", "--order-id", "10", "--perm-id", "12345"])
        with pytest.raises(SystemExit) as exc:
            ib_order_manage.main()
        assert exc.value.code == 1
        data = _emitted(capsys)
        assert data["status"] == "error"
        assert data["classification"] == "connection"

    def test_mid_modify_ibconnectionerror_emits_json(self, capsys, monkeypatch):
        """Same contract on the modify path — place_order raising
        IBConnectionError after lookup must still emit JSON."""
        t = make_trade(order_id=10, perm_id=12345, client_id=0)
        fake = make_client()
        fake.connect.return_value = None
        fake.get_open_orders.return_value = [t]
        fake.ib.errorEvent = ErrorEvent()
        fake.place_order = MagicMock(
            side_effect=IBConnectionError("Not connected to IB. Call connect() first.")
        )
        monkeypatch.setattr(ib_order_manage, "IBClient", lambda: fake)
        monkeypatch.setattr(
            sys,
            "argv",
            ["x", "modify", "--order-id", "10", "--perm-id", "12345", "--new-price", "22.50"],
        )
        with pytest.raises(SystemExit) as exc:
            ib_order_manage.main()
        assert exc.value.code == 1
        data = _emitted(capsys)
        assert data["status"] == "error"
        assert data["classification"] == "connection"


# ─── CLI plumbing ─────────────────────────────────────────────────────────


class TestIdentityArgsPlumbing:
    def test_cancel_accepts_account_and_client_id_args(self, capsys, monkeypatch):
        t = make_trade(order_id=10, perm_id=12345, client_id=0, account="DU0000000")
        fake = make_client()
        fake.connect.return_value = None
        fake.ib.errorEvent = ErrorEvent()
        fake.get_open_orders.side_effect = [[t], []]
        fake.cancel_order = MagicMock()
        monkeypatch.setattr(ib_order_manage, "IBClient", lambda: fake)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "x",
                "cancel",
                "--order-id",
                "10",
                "--perm-id",
                "12345",
                "--account",
                "DU0000000",
                "--client-id",
                "0",
            ],
        )
        with pytest.raises(SystemExit) as exc:
            ib_order_manage.main()
        assert exc.value.code == 0

    def test_cancel_cli_account_mismatch_emits_json(self, capsys, monkeypatch):
        t = make_trade(order_id=10, perm_id=12345, client_id=0, account="DU9999999")
        fake = make_client()
        fake.connect.return_value = None
        fake.get_open_orders.return_value = [t]
        fake.cancel_order = MagicMock()
        monkeypatch.setattr(ib_order_manage, "IBClient", lambda: fake)
        monkeypatch.setattr(
            sys,
            "argv",
            ["x", "cancel", "--order-id", "10", "--perm-id", "12345", "--account", "DU0000000"],
        )
        with pytest.raises(SystemExit) as exc:
            ib_order_manage.main()
        assert exc.value.code == 1
        data = _emitted(capsys)
        assert data["status"] == "error"
        assert data["classification"] == "ownership"
        fake.cancel_order.assert_not_called()
