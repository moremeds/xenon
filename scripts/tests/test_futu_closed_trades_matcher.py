"""FIFO lot-matcher: realized P&L, options multiplier, shorts, determinism, warnings."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from xenon.api.services.futu_closed_trades import closed_lots_to_rows, match_closed_lots


def _t(deal_id, side, qty, price, day, code="US.QQQ", ticker="QQQ"):
    return {
        "futu_deal_id": deal_id,
        "futu_code": code,
        "ticker": ticker,
        "quantity": qty,
        "price": price,
        "filled_at": datetime(2026, 6, day, tzinfo=timezone.utc),
        "raw": {"trd_side": side},
    }


def test_long_round_trip_realized_pnl():
    lots = match_closed_lots([_t("d1", "BUY", 10, 100, 1), _t("d2", "SELL", 10, 110, 2)])
    assert len(lots) == 1
    lot = lots[0]
    assert lot.action == "SELL"
    assert lot.quantity == Decimal("10")
    assert lot.realized_pnl == Decimal("100")  # (110-100)*10*1
    assert lot.cost_basis == Decimal("1000")
    assert lot.proceeds == Decimal("1100")
    assert lot.opened_at.day == 1 and lot.closed_at.day == 2
    assert lot.futu_close_id == "d2:d1"


def test_option_multiplier_applied():
    code, tk = "US.QQQ250620C500000", "QQQ250620C500000"
    lots = match_closed_lots([_t("d1", "BUY", 1, "3.48", 1, code, tk), _t("d2", "SELL", 1, "10.40", 2, code, tk)])
    assert lots[0].realized_pnl == Decimal("692.00")  # (10.40-3.48)*1*100


def test_short_round_trip():
    lots = match_closed_lots([_t("d1", "SELL_SHORT", 5, 50, 1), _t("d2", "BUY_BACK", 5, 40, 2)])
    assert lots[0].action == "BUY"
    assert lots[0].realized_pnl == Decimal("50")  # (50-40)*5
    assert lots[0].futu_close_id == "d2:d1"


def test_partial_close_across_two_open_lots_unique_ids():
    lots = match_closed_lots([_t("b1", "BUY", 5, 100, 1), _t("b2", "BUY", 5, 100, 1), _t("s1", "SELL", 10, 110, 2)])
    assert {l.futu_close_id for l in lots} == {"s1:b1", "s1:b2"}
    assert sum(l.realized_pnl for l in lots) == Decimal("100")  # (110-100)*10


def test_close_with_no_open_lot_warns_and_emits_nothing(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        lots = match_closed_lots([_t("s1", "SELL", 5, 100, 1)])
    assert lots == []
    assert any("no open lot" in r.message for r in caplog.records)


def test_unknown_side_skipped(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        lots = match_closed_lots([_t("x1", "WAT", 1, 1, 1)])
    assert lots == []
    assert any("unknown trd_side" in r.message for r in caplog.records)


def test_close_ids_stable_regardless_of_input_order():
    """Same-timestamp fills must produce identical close ids on a re-pull (shuffle-invariant)."""
    same_day = [
        _t("b1", "BUY", 5, 100, 1),
        _t("b2", "BUY", 5, 100, 1),
        _t("s1", "SELL", 10, 110, 1),
    ]
    ids_forward = {l.futu_close_id for l in match_closed_lots(same_day)}
    ids_reversed = {l.futu_close_id for l in match_closed_lots(list(reversed(same_day)))}
    assert ids_forward == ids_reversed


def test_closed_lots_to_rows_shape():
    lots = match_closed_lots([_t("d1", "BUY", 1, 100, 1), _t("d2", "SELL", 1, 150, 2)])
    rows = closed_lots_to_rows(lots)
    assert rows[0]["futu_close_id"] == "d2:d1"
    assert rows[0]["realized_pnl"] == Decimal("50")
    assert set(rows[0]) >= {"ticker", "action", "quantity", "cost_basis", "proceeds", "opened_at", "closed_at"}
