"""Same-timestamp fills produce deterministic P&L across input orderings."""

import random
from datetime import datetime
from decimal import Decimal

from xenon.execution.futu_fifo_matcher import FillInput, match_fills

T = "2024-05-01T10:00:00+00:00"


def _f(deal_id, action, qty, price):
    return FillInput(
        futu_deal_id=deal_id,
        action=action,
        quantity=qty,
        price=Decimal(str(price)),
        fees=Decimal("0"),
        filled_at=datetime.fromisoformat(T),
    )


def test_deterministic_across_input_orderings():
    base = [
        _f("a", "BUY", 50, 150),
        _f("b", "BUY", 50, 155),
        _f("c", "SELL", 75, 160),
    ]
    expected = [(f.futu_deal_id, f.realized_pnl) for f in match_fills(base, corp_actions=[])]
    rng = random.Random(0)
    for _ in range(20):
        shuffled = base[:]
        rng.shuffle(shuffled)
        got = [(f.futu_deal_id, f.realized_pnl) for f in match_fills(shuffled, corp_actions=[])]
        # Re-sort by deal_id for comparison since output order is event-time.
        assert sorted(got) == sorted(expected)
