"""trade_aggregator must not truncate fractional stock fill quantities."""

from __future__ import annotations

from decimal import Decimal

from xenon.execution.trade_aggregator import _quantity


def _fill(qty: str, side: str) -> dict:
    return {
        "qty": Decimal(qty),
        "side": side,
        "price": Decimal("703.34"),
        "ticker": "QQQ",
        "metadata": {"sec_type": "STK"},
        "con_id": 320227571,
    }


def test_quantity_keeps_fractions() -> None:
    fills = [_fill("0.4977", "BUY"), _fill("0.5023", "BUY")]
    assert _quantity(fills) == Decimal("1.0000")
