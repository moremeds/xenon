"""Past-date rows written by the backward walk carry nav_type='reconstructed'."""

from datetime import date
from decimal import Decimal

from xenon.api.services.futu_history import AnchorState, run_backward_walk
from xenon.db.queries.futu_history import fetch_nav_history
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_past_missing_rows_are_reconstructed(clean_scope):
    today = date(2024, 5, 4)
    anchor = AnchorState(today=today, cash=Decimal("0"), positions={})
    run_backward_walk(SCOPE, anchor, walk_to=date(2024, 5, 1))
    rows = list(fetch_nav_history(SCOPE, date(2024, 5, 1), today))
    # All historical rows reconstructed. Today's row is written separately
    # by write_today_reported_nav and is NOT covered by run_backward_walk.
    historical = [r for r in rows if r["date"] < today]
    assert len(historical) >= 1
    for r in historical:
        assert r["nav_type"] == "reconstructed"
