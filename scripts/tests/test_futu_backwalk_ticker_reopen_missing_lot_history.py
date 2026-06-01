"""When a SELL on D+1 fully closes a ticker that's empty in today's anchor,
the walk-back uses the FIFO lot_history side channel to recreate the lot.

A complete round trip with rematch-populated lot history yields the correct
pre-close NAV.
"""

from datetime import date, datetime
from decimal import Decimal

from xenon.api.services.futu_history import (
    AnchorState,
    _rematch_ticker,
    run_backward_walk,
)
from xenon.db.queries.futu_history import fetch_nav_history, insert_fills
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def _f(deal_id, action, ticker, qty, price, t):
    return dict(
        futu_deal_id=deal_id,
        futu_order_id="o",
        ticker=ticker,
        futu_code=f"US.{ticker}",
        market="US",
        action=action,
        quantity=qty,
        price=Decimal(str(price)),
        fees=Decimal("0"),
        filled_at=datetime.fromisoformat(t),
        raw={},
    )


def test_walk_back_uses_lot_history_for_closed_ticker(clean_scope):
    insert_fills(
        SCOPE,
        [
            _f("d1", "BUY", "AAPL", 50, 100, "2024-05-01T14:00:00+00:00"),
            _f("d2", "SELL", "AAPL", 50, 110, "2024-05-02T14:00:00+00:00"),
        ],
    )
    # Populate _LOT_HISTORY_CACHE for AAPL via rematch (mirrors what the
    # orchestrator does in step 5 before calling run_backward_walk).
    _rematch_ticker(SCOPE, "AAPL")

    # Anchor (END-OF-5/02): cash=$5,500 (sold AAPL), positions empty.
    anchor = AnchorState(today=date(2024, 5, 2), cash=Decimal("5500"), positions={})
    run_backward_walk(SCOPE, anchor, walk_to=date(2024, 5, 1))
    rows = {r["date"]: r for r in fetch_nav_history(SCOPE, date(2024, 5, 1), date(2024, 5, 2))}
    # D had 50 sh @ $100 + $0 cash → $5,000.
    assert rows[date(2024, 5, 1)]["nav"] == Decimal("5000")
