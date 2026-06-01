"""Splits / reverse splits applied mid-hold preserve total realized P&L."""

from datetime import date, datetime
from decimal import Decimal

from xenon.execution.futu_fifo_matcher import (
    CorpActionInput,
    FillInput,
    match_fills,
)


def _f(deal_id, action, qty, price, t):
    return FillInput(
        futu_deal_id=deal_id,
        action=action,
        quantity=qty,
        price=Decimal(str(price)),
        fees=Decimal("0"),
        filled_at=datetime.fromisoformat(t),
    )


def _ca(id_, type_, d, num, den):
    return CorpActionInput(
        id=id_,
        action_type=type_,
        effective_date=date.fromisoformat(d),
        ratio_numerator=Decimal(str(num)),
        ratio_denominator=Decimal(str(den)),
    )


def test_ten_for_one_split_preserves_total_realized():
    fills = [
        _f("1", "BUY", 10, 1000, "2024-05-01T10:00:00+00:00"),
        # After 10:1 split on 6/10, the position is 100 sh @ $100 cost.
        _f("2", "SELL", 100, 110, "2024-06-15T10:00:00+00:00"),
    ]
    corp = [_ca("ca1", "split", "2024-06-10", 10, 1)]
    out = match_fills(fills, corp_actions=corp)
    # gross = (110 - 100) * 100 = 1000
    assert out[1].realized_pnl == Decimal("1000.00")


def test_reverse_split_1_for_5():
    fills = [
        _f("1", "BUY", 500, 2, "2024-05-01T10:00:00+00:00"),
        # After 1:5 reverse split: 100 sh @ $10 cost
        _f("2", "SELL", 100, 12, "2024-06-15T10:00:00+00:00"),
    ]
    corp = [_ca("ca1", "reverse_split", "2024-06-10", 1, 5)]
    out = match_fills(fills, corp_actions=corp)
    # gross = (12 - 10) * 100 = 200
    assert out[1].realized_pnl == Decimal("200.00")


def test_split_on_same_day_sorts_after_fill():
    # BUY on 2024-06-10 morning → split same date → SELL on 2024-06-15.
    # Spec: fills sort BEFORE corp actions on same date.
    fills = [
        _f("1", "BUY", 10, 1000, "2024-06-10T09:30:00+00:00"),
        _f("2", "SELL", 100, 110, "2024-06-15T10:00:00+00:00"),
    ]
    corp = [_ca("ca1", "split", "2024-06-10", 10, 1)]
    out = match_fills(fills, corp_actions=corp)
    assert out[1].realized_pnl == Decimal("1000.00")


def test_two_sequential_splits():
    # Initial: 10 @ $100. Split 2:1 on 6/10 → 20 @ $50. Split 5:1 on 7/01
    # → 100 @ $10. SELL 100 @ $12 → P&L = 200.
    fills = [
        _f("1", "BUY", 10, 100, "2024-05-01T10:00:00+00:00"),
        _f("2", "SELL", 100, 12, "2024-07-15T10:00:00+00:00"),
    ]
    corp = [
        _ca("ca1", "split", "2024-06-10", 2, 1),
        _ca("ca2", "split", "2024-07-01", 5, 1),
    ]
    out = match_fills(fills, corp_actions=corp)
    assert out[1].realized_pnl == Decimal("200.00")
