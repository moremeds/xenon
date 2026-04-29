"""Flex CLI must surface perm_id (Task 0)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from xenon.trade_blotter.cli import blotter_to_dict
from xenon.trade_blotter.models import Execution, SecurityType, Side, Trade, TradeBlotter


def _make_blotter_with_perm_id(perm_id: str | None) -> TradeBlotter:
    exec_in = Execution(
        exec_id="E1",
        time=datetime(2026, 4, 27, 14, 30),
        symbol="AAPL",
        sec_type=SecurityType.STOCK,
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("100"),
        commission=Decimal("0.5"),
        perm_id=perm_id,
    )
    exec_out = Execution(
        exec_id="E2",
        time=datetime(2026, 4, 27, 15, 30),
        symbol="AAPL",
        sec_type=SecurityType.STOCK,
        side=Side.SELL,
        quantity=Decimal("1"),
        price=Decimal("110"),
        commission=Decimal("0.5"),
        perm_id=perm_id,
    )
    trade = Trade(
        symbol="AAPL", contract_desc="AAPL (STK)", sec_type=SecurityType.STOCK, executions=[exec_in, exec_out]
    )
    return TradeBlotter(trades=[trade], as_of=datetime(2026, 4, 27, 16, 0))


def test_flex_payload_includes_perm_id_per_trade():
    blotter = _make_blotter_with_perm_id("PERM-X")
    payload = blotter_to_dict(blotter)
    closed = payload["closed_trades"]
    assert closed
    assert closed[0]["perm_id"] == "PERM-X"


def test_flex_payload_perm_id_none_when_missing():
    blotter = _make_blotter_with_perm_id(None)
    payload = blotter_to_dict(blotter)
    closed = payload["closed_trades"]
    assert closed
    assert closed[0]["perm_id"] is None
