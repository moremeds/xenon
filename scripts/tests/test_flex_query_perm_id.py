"""Flex CLI must surface perm_id (Task 0)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from xenon.trade_blotter.cli import blotter_to_dict
from xenon.trade_blotter.flex_query import group_executions_to_trades
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


def _exec(exec_id: str, side: Side, price: str, *, perm_id: str | None, ib_order_id: str | None, hour: int) -> Execution:
    return Execution(
        exec_id=exec_id,
        time=datetime(2026, 4, 27, hour, 0),
        symbol="AAPL",
        sec_type=SecurityType.STOCK,
        side=side,
        quantity=Decimal("1"),
        price=Decimal(price),
        commission=Decimal("0.5"),
        perm_id=perm_id,
        ib_order_id=ib_order_id,
    )


def test_flex_grouping_keeps_same_contract_distinct_by_perm_id():
    trades = group_executions_to_trades(
        [
            _exec("E1", Side.BUY, "100", perm_id="PERM-1", ib_order_id="ORD-1", hour=10),
            _exec("E2", Side.SELL, "110", perm_id="PERM-1", ib_order_id="ORD-1", hour=11),
            _exec("E3", Side.BUY, "120", perm_id="PERM-2", ib_order_id="ORD-2", hour=12),
            _exec("E4", Side.SELL, "130", perm_id="PERM-2", ib_order_id="ORD-2", hour=13),
        ]
    )

    payload = blotter_to_dict(TradeBlotter(trades=trades, as_of=datetime(2026, 4, 27, 16, 0)))

    assert [row["perm_id"] for row in payload["closed_trades"]] == ["PERM-1", "PERM-2"]


def test_flex_grouping_uses_ib_order_id_only_as_group_key_not_perm_id():
    trades = group_executions_to_trades(
        [
            _exec("E1", Side.BUY, "100", perm_id=None, ib_order_id="ORD-1", hour=10),
            _exec("E2", Side.SELL, "110", perm_id=None, ib_order_id="ORD-1", hour=11),
            _exec("E3", Side.BUY, "120", perm_id=None, ib_order_id="ORD-2", hour=12),
            _exec("E4", Side.SELL, "130", perm_id=None, ib_order_id="ORD-2", hour=13),
        ]
    )

    payload = blotter_to_dict(TradeBlotter(trades=trades, as_of=datetime(2026, 4, 27, 16, 0)))

    assert len(payload["closed_trades"]) == 2
    assert [row["perm_id"] for row in payload["closed_trades"]] == [None, None]
