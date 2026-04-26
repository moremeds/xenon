"""Scope regressions for the sync IB execution logger."""

from __future__ import annotations

from types import SimpleNamespace


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
