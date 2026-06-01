"""Golden-day cases for the backward walk.

The walk starts from today's anchor and processes dates N-1, N-2, … by
un-applying each day's events to compute the END-OF-(N-1) state. Each
historical NAV row is written with nav_type='reconstructed'.
"""

from datetime import date, datetime
from decimal import Decimal

from xenon.api.services.futu_history import AnchorState, run_backward_walk
from xenon.db.queries.futu_history import (
    fetch_nav_history,
    insert_cash_flows,
    insert_fills,
)
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def _fill(deal_id, action, ticker, qty, price, t):
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


def _cf(flow_id, amount, t, flow_type="deposit"):
    return dict(
        futu_flow_id=flow_id,
        flow_type=flow_type,
        futu_type_raw=flow_type.upper(),
        amount=Decimal(str(amount)),
        amount_original=Decimal(str(amount)),
        original_currency="USD",
        fx_rate_used=None,
        ticker=None,
        description="",
        flow_at=datetime.fromisoformat(t),
        raw={},
    )


def test_pure_cash_deposit_walks_to_zero(clean_scope):
    # Today: cash=$1000, no positions. Yesterday: deposit of $1000 → cash[D-1]=0.
    insert_cash_flows(SCOPE, [_cf("f1", 1000, "2024-05-02T14:00:00+00:00")])
    anchor = AnchorState(today=date(2024, 5, 2), cash=Decimal("1000"), positions={})
    run_backward_walk(SCOPE, anchor, walk_to=date(2024, 5, 1))
    rows = list(fetch_nav_history(SCOPE, date(2024, 5, 1), date(2024, 5, 2)))
    by_d = {r["date"]: r for r in rows}
    assert by_d[date(2024, 5, 1)]["nav"] == Decimal("0")
    assert by_d[date(2024, 5, 1)]["nav_type"] == "reconstructed"


def test_single_round_trip_returns_open_state(clean_scope):
    # Day D=5/01: BUY 100 @ 150 ($15,000 spent). Day D+1=5/02: SELL 100 @ 160.
    # Anchor (END-OF-5/02): cash=$16,000 (incl realized), positions empty.
    # Walk back: un-apply SELL on 5/02 → cash[5/01] = 16000 - 16000 = 0,
    # positions[5/01] = {AAPL: 100@150}. nav[5/01] = 0 + 100*150 = 15000.
    insert_fills(
        SCOPE,
        [
            _fill("d1", "BUY", "AAPL", 100, 150, "2024-05-01T14:00:00+00:00"),
            _fill("d2", "SELL", "AAPL", 100, 160, "2024-05-02T14:00:00+00:00"),
        ],
    )
    anchor = AnchorState(today=date(2024, 5, 2), cash=Decimal("16000"), positions={})
    run_backward_walk(SCOPE, anchor, walk_to=date(2024, 5, 1))
    rows = list(fetch_nav_history(SCOPE, date(2024, 5, 1), date(2024, 5, 2)))
    by_d = {r["date"]: r for r in rows}
    # D had 100 sh @ avg_cost 150 + 0 cash = 15000.
    assert by_d[date(2024, 5, 1)]["nav"] == Decimal("15000")
