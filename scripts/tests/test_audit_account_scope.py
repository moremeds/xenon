"""Naked-short audit only judges and cancels the scope account's orders.

PR #198 review (Astra P1-1, Fable confirmed): after positions were scoped to
the scope account, open orders were still read for every managed account. A
covered SELL call on the OTHER account (that account holds the shares) then
looked naked against the scope account's positions, and the audit cancelled
it. Fake IB only — no network.
"""

from __future__ import annotations

import contextlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from xenon.execution import naked_short_audit

SCOPE = "DU1111111"
OTHER = "DU2222222"


def _trade(account: str, *, perm_id: int = 500, order_id: int = 5):
    """Open SELL 1 AAPL call — covered on OTHER, which holds 100 shares."""
    return SimpleNamespace(
        order=SimpleNamespace(
            account=account, action="SELL", totalQuantity=1, orderId=order_id, permId=perm_id, orderRef=""
        ),
        contract=SimpleNamespace(
            secType="OPT", symbol="AAPL", right="C", lastTradeDateOrContractMonth="20261120", strike=250
        ),
        orderStatus=SimpleNamespace(status="Submitted"),
    )


# The scope account holds nothing; the other account's 100 shares are not
# in the scope snapshot.
SCOPE_POSITIONS: list = []


@pytest.fixture(autouse=True)
def _scope_env(monkeypatch):
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", SCOPE)


# ---------------------------------------------------------------------------
# cancel_violations refuses another account's order
# ---------------------------------------------------------------------------


def test_cancel_violations_skips_other_account_trade():
    client = MagicMock()
    client.get_open_orders.return_value = [_trade(OTHER)]
    violations = [{"order_id": 5, "perm_id": 500, "symbol": "AAPL", "reason": "test"}]

    assert naked_short_audit.cancel_violations(client, violations, account=SCOPE) == 0
    client.cancel_order.assert_not_called()


def test_cancel_violations_cancels_scope_account_trade():
    client = MagicMock()
    client.get_open_orders.return_value = [_trade(SCOPE)]
    violations = [{"order_id": 5, "perm_id": 500, "symbol": "AAPL", "reason": "test"}]

    assert naked_short_audit.cancel_violations(client, violations, account=SCOPE) == 1
    client.cancel_order.assert_called_once()


# ---------------------------------------------------------------------------
# Inline audit in ib_sync
# ---------------------------------------------------------------------------


def test_ib_sync_audit_orders_keep_only_scope_account():
    from xenon.execution import ib_sync

    client = SimpleNamespace(get_open_orders=lambda: [_trade(OTHER), _trade(""), _trade(SCOPE, perm_id=7)])

    orders = ib_sync._open_orders_for_audit(client, SCOPE)

    assert [o["permId"] for o in orders] == [7]


def test_ib_sync_inline_audit_other_account_covered_sell_no_violation(monkeypatch):
    from xenon.execution import ib_sync

    client = MagicMock()
    client.ib.managedAccounts.return_value = [SCOPE, OTHER]
    client.get_open_orders.return_value = [_trade(OTHER)]
    client.is_connected.return_value = True
    client.ib.reqPnL.return_value = None
    monkeypatch.setattr(ib_sync, "connect_ib", lambda *a, **k: client)
    monkeypatch.setattr(ib_sync, "get_account_summary", lambda _c, _a: {})
    monkeypatch.setattr(ib_sync, "get_fx_rates", lambda _c, _a: {"USD": 1.0})
    monkeypatch.setattr(ib_sync, "fetch_positions", lambda _c, _a=None, **_k: [])
    monkeypatch.setattr(ib_sync, "build_fill_dates", lambda _c, **_k: {})
    # PR-03b sync pass: the same open-order read also feeds the PG mirror;
    # that path is covered by test_gate4_sync_pass — stub it here.
    from xenon.execution import ib_orders

    monkeypatch.setattr(ib_orders, "open_orders_from_trades", lambda _c, _t: [])
    monkeypatch.setattr(ib_sync, "_persist_pass_activity", lambda *_a, **_k: True)
    monkeypatch.setattr(ib_sync, "convert_to_portfolio_format", lambda *a, **k: {"positions": SCOPE_POSITIONS})
    monkeypatch.setattr(ib_sync, "save_portfolio", lambda _p: None)
    # ib_sync imports the audit by bare module name; provide it.
    monkeypatch.setitem(sys.modules, "naked_short_audit", naked_short_audit)
    cancels = []
    monkeypatch.setattr(naked_short_audit, "cancel_violations", lambda *a, **k: cancels.append(a) or 0)
    found = []
    real_find = naked_short_audit.find_naked_short_violations
    monkeypatch.setattr(
        naked_short_audit,
        "find_naked_short_violations",
        lambda orders, positions: found.extend(real_find(orders, positions)) or list(found),
    )
    monkeypatch.setattr(sys, "argv", ["xenon-ib-sync", "--sync", "--no-prices"])

    ib_sync.main()

    assert found == []
    assert cancels == []
    client.cancel_order.assert_not_called()


# ---------------------------------------------------------------------------
# Standalone xenon-naked-short-audit
# ---------------------------------------------------------------------------


@pytest.fixture
def portfolio_file(tmp_path):
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps({"positions": SCOPE_POSITIONS}))
    return str(path)


@pytest.fixture
def fake_ib(monkeypatch):
    clients = []

    class _FakeIBClient:
        def __init__(self):
            self.cancelled = []
            clients.append(self)

        def connect(self, **_kw):
            return None

        def get_open_orders(self):
            return [_trade(OTHER)]

        def cancel_order(self, order):
            self.cancelled.append(order)

        def sleep(self, _s):
            return None

        def disconnect(self):
            return None

    monkeypatch.setattr(naked_short_audit, "IBClient", _FakeIBClient)
    monkeypatch.setattr(naked_short_audit, "acquire_owner", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr("xenon.db.service_health.record_service_health", lambda *a, **k: None)
    return clients


def test_standalone_default_path_ignores_other_account_order(portfolio_file, fake_ib):
    summary = naked_short_audit.main(["--portfolio", portfolio_file])

    assert summary["violations_found"] == 0
    assert summary["cancelled"] == 0
    assert all(c.cancelled == [] for c in fake_ib)


def test_standalone_file_path_ignores_other_account_and_accountless_orders(tmp_path, portfolio_file, fake_ib):
    def _order(account):
        o = naked_short_audit._shape_open_order_for_audit(_trade(account))
        if account is None:
            o.pop("account")
        return o

    orders = tmp_path / "orders.json"
    orders.write_text(json.dumps({"open_orders": [_order(OTHER), _order(None)]}))

    summary = naked_short_audit.main(["--portfolio", portfolio_file, "--orders", str(orders)])

    assert summary["violations_found"] == 0
    assert summary["cancelled"] == 0
    assert fake_ib == []  # no IB session opened, nothing cancelled


def test_standalone_unresolved_scope_skips_audit(monkeypatch, portfolio_file, fake_ib):
    monkeypatch.delenv("XENON_BROKER_ACCOUNT", raising=False)

    summary = naked_short_audit.main(["--portfolio", portfolio_file])

    assert summary["cancelled"] == 0
    assert summary["violations_found"] == 0
    assert "unresolved" in summary["error"]
    assert fake_ib == []
