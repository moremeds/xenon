"""A 10:1 split on D+1 means walk-back qty(D) = qty(D+1) / 10,
avg_cost(D) = avg_cost(D+1) * 10.
"""

from datetime import date
from decimal import Decimal

from xenon.api.services.futu_history import (
    AnchorState,
    _PositionState,
    run_backward_walk,
)
from xenon.db.queries.futu_history import (
    fetch_nav_history,
    insert_corporate_actions,
)
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_split_reverses_on_walk_back(clean_scope):
    # Anchor (D+1 post-split): 100 sh @ avg_cost $10 + $0 cash → NAV = $1000.
    insert_corporate_actions(
        [
            {
                "ticker": "NVDA",
                "action_type": "split",
                "effective_date": date(2024, 6, 10),
                "ratio_numerator": Decimal("10"),
                "ratio_denominator": Decimal("1"),
                "new_ticker": None,
                "notes": {},
            }
        ]
    )
    anchor = AnchorState(
        today=date(2024, 6, 10),
        cash=Decimal("0"),
        positions={"NVDA": _PositionState(qty=Decimal("100"), avg_cost=Decimal("10"))},
    )
    run_backward_walk(SCOPE, anchor, walk_to=date(2024, 6, 9))
    rows = {r["date"]: r for r in fetch_nav_history(SCOPE, date(2024, 6, 9), date(2024, 6, 10))}
    # D-1 (pre-split): qty = 10, avg_cost = $100 → still NAV $1000.
    assert rows[date(2024, 6, 9)]["nav"] == Decimal("1000")
