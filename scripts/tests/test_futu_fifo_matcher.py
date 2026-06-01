"""Core FIFO matcher tests — fills only. Corp actions covered in Task 8."""

from datetime import datetime
from decimal import Decimal

from xenon.execution.futu_fifo_matcher import FillInput, match_fills


def _f(deal_id, action, qty, price, fees="0", t="2024-05-01T10:00:00+00:00"):
    return FillInput(
        futu_deal_id=deal_id,
        action=action,
        quantity=qty,
        price=Decimal(str(price)),
        fees=Decimal(str(fees)),
        filled_at=datetime.fromisoformat(t),
    )


def test_opening_fill_realized_pnl_is_none():
    fills = [_f("1", "BUY", 100, 150)]
    out = match_fills(fills, corp_actions=[])
    assert out[0].realized_pnl is None


def test_full_close_one_lot():
    fills = [
        _f("1", "BUY", 100, 150, fees="1.00"),
        _f("2", "SELL", 100, 160, fees="1.00", t="2024-05-02T10:00:00+00:00"),
    ]
    out = match_fills(fills, corp_actions=[])
    # gross = (160-150)*100 = 1000; net of $1 sell fee + $1 buy fee = 998
    assert out[1].realized_pnl == Decimal("998.00")


def test_partial_close_across_two_lots():
    fills = [
        _f("1", "BUY", 50, 150, fees="0.50"),
        _f("2", "BUY", 50, 155, fees="0.50", t="2024-05-02T10:00:00+00:00"),
        _f("3", "SELL", 75, 160, fees="0.75", t="2024-05-03T10:00:00+00:00"),
    ]
    out = match_fills(fills, corp_actions=[])
    # Closes 50@150 fully + 25@155 partially.
    # gross = (160-150)*50 + (160-155)*25 = 500 + 125 = 625
    # buy fee allocation = 0.50 (full lot1) + 0.50*(25/50) (half of lot2) = 0.75
    # net = 625 - 0.75 (sell) - 0.75 (buy) = 623.50
    assert out[2].realized_pnl == Decimal("623.50")


def test_over_close_logs_warning_no_raise(caplog):
    fills = [
        _f("1", "BUY", 50, 150),
        _f("2", "SELL", 100, 160, t="2024-05-02T10:00:00+00:00"),
    ]
    with caplog.at_level("WARNING"):
        out = match_fills(fills, corp_actions=[])
    assert any("closed more than opened" in r.message.lower() for r in caplog.records)
    # gross = (160-150)*50 = 500, no fees → 500
    assert out[1].realized_pnl == Decimal("500.00")


def test_lot_history_side_channel_populated():
    fills = [
        _f("1", "BUY", 100, 150),
        _f("2", "SELL", 100, 160, t="2024-05-02T10:00:00+00:00"),
    ]
    out = match_fills(fills, corp_actions=[], return_lot_history=True)
    assert hasattr(out, "fills") and hasattr(out, "lot_history")
    assert any(ev["kind"] == "open" for ev in out.lot_history)
    assert any(ev["kind"] == "close" for ev in out.lot_history)
