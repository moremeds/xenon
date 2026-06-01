"""Pin _unapply_buy + _unapply_sell signs against the forward FIFO (C18).

Two-direction regression: if a future "simpler" refactor flips a sign,
these failures point at exactly which one.
"""

from decimal import Decimal

from xenon.api.services.futu_history import (
    _PositionState,
    _unapply_buy,
    _unapply_sell,
)


def test_unapply_buy_subtracts_qty():
    # END-OF-(D+1) state: long 100 sh @ avg_cost 150.
    # BUY on D+1: 10 sh @ 160. END-OF-D = 90 sh @ pre_total/90.
    post = _PositionState(qty=Decimal("100"), avg_cost=Decimal("150"))
    pre = _unapply_buy(post, qty=Decimal("10"), price=Decimal("160"), fees=Decimal("1"))
    assert pre.qty == Decimal("90")
    # post total = 100*150 = 15000; minus 10*160 = 1600; remaining 13400 / 90.
    assert (pre.avg_cost - Decimal("148.8889")).copy_abs() < Decimal("0.001")


def test_unapply_sell_adds_qty_keeps_avg_cost():
    post = _PositionState(qty=Decimal("50"), avg_cost=Decimal("100"))
    pre = _unapply_sell(post, qty=Decimal("25"))
    assert pre.qty == Decimal("75")
    assert pre.avg_cost == Decimal("100")


def test_unapply_buy_to_zero_returns_zero_avg_cost():
    """BUY of exactly the held quantity: pre-state qty=0, avg_cost=0 sentinel."""
    post = _PositionState(qty=Decimal("10"), avg_cost=Decimal("150"))
    pre = _unapply_buy(post, qty=Decimal("10"), price=Decimal("160"), fees=Decimal("0"))
    assert pre.qty == Decimal("0")
    assert pre.avg_cost == Decimal("0")
