"""Order/fill mirrors write only the scope account's IB rows.

PR #198 review (Fable P2): reqAllOpenOrders and reqExecutions span every
managed account, but `ib_orders --sync` / the activity poller
(sync_open_orders_to_postgres) and the fills mirror (record_external_fills:
boot replay, poller tick, xenon-ib-reconcile) stamped every row with the
scope account. Rows naming another account, or an empty account, are now
dropped. A row dict with no account key at all (not produced from an IB
Order/Execution, which always carry account/acctNumber) is kept.
Fake IB and stubbed writers only — no network, no DB writes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope(broker="IB", account_env="paper", broker_account="DU1111111")
OTHER = "DU2222222"


# ---------------------------------------------------------------------------
# Open orders: fetch_open_orders + sync_open_orders_to_postgres
# ---------------------------------------------------------------------------


def _trade(account: str, perm_id: int):
    return SimpleNamespace(
        order=SimpleNamespace(
            account=account,
            orderId=perm_id,
            permId=perm_id,
            action="SELL",
            orderType="LMT",
            totalQuantity=1,
            lmtPrice=1.0,
            auxPrice=0.0,
            tif="GTC",
            orderRef="",
        ),
        contract=SimpleNamespace(
            secType="STK", symbol="AAPL", conId=1, currency="USD", exchange="SMART", comboLegs=None
        ),
        orderStatus=SimpleNamespace(status="Submitted", filled=0, remaining=1, avgFillPrice=0.0),
    )


def test_open_order_sync_writes_only_scope_account(monkeypatch):
    from xenon.execution import ib_orders, orders_store

    client = SimpleNamespace(
        get_open_orders=lambda: [_trade("DU1111111", 11), _trade(OTHER, 22), _trade("", 33)],
        qualify_contracts=lambda *c: [],
    )
    written = []
    monkeypatch.setattr(
        orders_store, "register_from_snapshot", lambda **kw: written.append(kw["perm_id"]) or {"action": "INSERTED"}
    )

    open_orders = ib_orders.fetch_open_orders(client)
    result = ib_orders.sync_open_orders_to_postgres(open_orders, scope=SCOPE)

    assert [o["account"] for o in open_orders] == ["DU1111111", OTHER, ""]
    assert written == ["11"]
    assert result["registered"] == 1


def test_open_order_sync_keeps_dict_without_account_key(monkeypatch):
    from xenon.execution import ib_orders, orders_store

    written = []
    monkeypatch.setattr(
        orders_store, "register_from_snapshot", lambda **kw: written.append(kw["perm_id"]) or {"action": "INSERTED"}
    )

    ib_orders.sync_open_orders_to_postgres(
        [{"permId": 44, "orderId": 44, "action": "BUY", "totalQuantity": 1, "contract": {"secType": "STK"}}],
        scope=SCOPE,
    )

    assert written == ["44"]


# ---------------------------------------------------------------------------
# Fills: fetch_ib_executions + record_external_fills
# ---------------------------------------------------------------------------


def _fill(account: str, exec_id: str):
    return SimpleNamespace(
        execution=SimpleNamespace(
            execId=exec_id,
            permId=7,
            orderId=7,
            acctNumber=account,
            time=datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc),
            side="BOT",
            shares=1,
            price=1.0,
            exchange="SMART",
        ),
        contract=SimpleNamespace(
            conId=1, symbol="AAPL", secType="STK", strike=0, lastTradeDateOrContractMonth="", right=""
        ),
        commissionReport=None,
    )


@pytest.fixture
def fill_writes(monkeypatch):
    from xenon.execution import ib_reconcile

    written = []
    monkeypatch.setattr(ib_reconcile, "_resolve_submission_id_by_perm_id", lambda *_a, **_k: None)
    monkeypatch.setattr(ib_reconcile, "record_fill", lambda **kw: written.append(kw["exec_id"]) or True)
    monkeypatch.setattr(ib_reconcile, "aggregate_trade_from_fills", lambda **_k: None)
    return written


def test_fills_mirror_writes_only_scope_account(fill_writes):
    from xenon.execution import ib_reconcile

    client = SimpleNamespace(
        get_fills=lambda: [_fill("DU1111111", "e-scope"), _fill(OTHER, "e-other"), _fill("", "e-blank")]
    )

    executions = ib_reconcile.fetch_ib_executions(client)
    result = ib_reconcile.record_external_fills(executions, scope=SCOPE)

    assert [e["account"] for e in executions] == ["DU1111111", OTHER, ""]
    assert fill_writes == ["e-scope"]
    assert result["inserted"] == 1


def test_fills_mirror_keeps_dict_without_account_key(fill_writes):
    from xenon.execution import ib_reconcile

    execution = {
        "exec_id": "e-legacy",
        "perm_id": 8,
        "symbol": "AAPL",
        "side": "BOT",
        "shares": 1,
        "price": 1.0,
        "time": datetime(2026, 10, 16, 15, 0, tzinfo=timezone.utc),
    }

    ib_reconcile.record_external_fills([execution], scope=SCOPE)

    assert fill_writes == ["e-legacy"]
