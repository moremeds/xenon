"""Scope regressions for the sync IB execution logger."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select

from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_fills, trades


def _filled_result() -> dict:
    return {
        "status": "filled",
        "symbol": "AAPL",
        "order_id": 123,
        "quantity": 1,
        "avg_price": 100.0,
        "total_value": 100.0,
        "commission": 1.0,
        "fills": [],
    }


def test_log_trade_rejects_partial_scope_env(monkeypatch):
    from xenon.execution import ib_execute

    def fail_if_called():
        raise AssertionError("partial scope must fail before opening a DB engine")

    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.delenv("XENON_BROKER_ACCOUNT", raising=False)
    monkeypatch.setattr(ib_execute, "get_sync_engine", fail_if_called)

    executor = ib_execute.OrderExecutor("localhost", 4002, 25)
    contract = SimpleNamespace(symbol="AAPL")

    assert executor.log_trade(_filled_result(), contract, "BUY", 100.0) is False


def test_log_trade_writes_legacy_cli_fill_and_derived_trade(monkeypatch):
    from xenon.execution import ib_execute

    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU123456")

    executor = ib_execute.OrderExecutor("localhost", 4002, 25)
    contract = SimpleNamespace(symbol="AAPL", conId=265598)
    result = _filled_result()
    result["fills"] = [
        {
            "exec_id": "exec-cli-001",
            "con_id": 265598,
            "ticker": "AAPL",
            "side": "BUY",
            "qty": 1,
            "price": 100.0,
            "commission": 1.0,
            "time": datetime(2026, 4, 28, 14, 30, tzinfo=timezone.utc),
        }
    ]

    assert executor.log_trade(result, contract, "BUY", 100.0) is True

    engine = get_sync_engine()
    with engine.connect() as conn:
        fill = conn.execute(select(order_fills).where(order_fills.c.exec_id == "exec-cli-001")).one()._mapping
        trade = conn.execute(select(trades).where(trades.c.metadata["legacy_id"].astext == "exec-cli-001")).one()._mapping

    assert fill["submission_id"] is None
    assert fill["metadata"]["legacy_source"] == "ib_execute_cli"
    assert fill["metadata"]["legacy_id"] == "exec-cli-001"
    assert fill["account_env"] == "paper"
    assert fill["broker_account"] == "DU123456"
    assert trade["submission_id"] is None
    assert trade["ticker"] == "AAPL"
    assert trade["action"] == "BUY"
    assert trade["quantity"] == 1
    assert trade["entry_cost"] == Decimal("101.0000")
