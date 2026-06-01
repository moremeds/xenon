"""Pre-seeded partial nav_history row in the middle of the window: the walk
must NOT skip state advancement on that date — it still un-applies events,
just leaves the pre-existing row intact (immutable historical).
"""

from datetime import date
from decimal import Decimal

from xenon.api.services.futu_history import AnchorState, run_backward_walk
from xenon.db.queries.futu_history import fetch_nav_history, write_nav_row
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_existing_row_does_not_break_downstream_state(clean_scope):
    # Pre-seed an unrelated nav_history row mid-window.
    write_nav_row(
        SCOPE,
        date(2024, 5, 1),
        Decimal("9999"),
        "reconstructed",
        Decimal("9999"),
        Decimal("0"),
    )
    anchor = AnchorState(today=date(2024, 5, 3), cash=Decimal("1000"), positions={})
    run_backward_walk(SCOPE, anchor, walk_to=date(2024, 5, 1))
    rows = {r["date"]: r for r in fetch_nav_history(SCOPE, date(2024, 5, 1), date(2024, 5, 3))}
    # 5/01 row was immutable — must stay at 9999.
    assert rows[date(2024, 5, 1)]["nav"] == Decimal("9999")
    # 5/02 row was written by the walk with no fills/flows — state unchanged
    # from anchor: cash=1000, positions={} → nav=1000.
    assert rows[date(2024, 5, 2)]["nav"] == Decimal("1000")
