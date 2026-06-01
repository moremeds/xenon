"""A fee event that references a deal_id is dropped at *query time*, not
ingest time. Order-of-arrival irrelevant.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from xenon.api.services.futu_history import sum_cash_flow_for_date
from xenon.db.queries.futu_history import insert_cash_flows, insert_fills
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")
D = date(2024, 5, 1)


def _fill(deal_id):
    return dict(
        futu_deal_id=deal_id,
        futu_order_id="o",
        ticker="AAPL",
        futu_code="US.AAPL",
        market="US",
        action="BUY",
        quantity=1,
        price=Decimal("100"),
        fees=Decimal("1"),
        filled_at=datetime.fromisoformat("2024-05-01T14:00:00+00:00"),
        raw={},
    )


def _fee_cf(flow_id, deal_id):
    return dict(
        futu_flow_id=flow_id,
        flow_type="fee",
        futu_type_raw="COMMISSION",
        amount=Decimal("-1"),
        amount_original=Decimal("-1"),
        original_currency="USD",
        fx_rate_used=None,
        ticker="AAPL",
        description="commission",
        flow_at=datetime.fromisoformat("2024-05-01T14:00:00+00:00"),
        raw={"deal_id": deal_id},
    )


def test_fee_then_deal(clean_scope):
    insert_cash_flows(SCOPE, [_fee_cf("f1", "d1")])
    insert_fills(SCOPE, [_fill("d1")])
    # Cash-flow sum for the date should EXCLUDE the fee row because deal_id
    # 'd1' now exists as a fill (whose .fees column already accounts for it).
    assert sum_cash_flow_for_date(SCOPE, D) == Decimal("0")


def test_deal_then_fee(clean_scope):
    insert_fills(SCOPE, [_fill("d1")])
    insert_cash_flows(SCOPE, [_fee_cf("f1", "d1")])
    assert sum_cash_flow_for_date(SCOPE, D) == Decimal("0")


def test_fee_without_matching_deal_kept(clean_scope):
    """A fee whose raw.deal_id doesn't match any fill row remains in the sum."""
    insert_cash_flows(SCOPE, [_fee_cf("f1", "d_unknown")])
    assert sum_cash_flow_for_date(SCOPE, D) == Decimal("-1")
