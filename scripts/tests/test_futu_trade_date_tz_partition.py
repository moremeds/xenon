"""A 21:30 ET fill (= 01:30 UTC next day) buckets to the ET trade date,
even when the Postgres session runs in UTC.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

from xenon.db.queries.futu_history import fetch_fills, insert_fills
from xenon.execution.account_scope import AccountScope

SCOPE = AccountScope("FUTU", "paper", "pytest")


def test_2130_et_fill_buckets_to_et_date(clean_scope):
    # 2024-05-01 21:30 ET == 2024-05-02 01:30 UTC.
    fill = dict(
        futu_deal_id="d1",
        futu_order_id="o",
        ticker="AAPL",
        futu_code="US.AAPL",
        market="US",
        action="BUY",
        quantity=1,
        price=Decimal("100"),
        fees=Decimal("0"),
        filled_at=datetime(2024, 5, 2, 1, 30, tzinfo=timezone.utc),
        raw={},
    )
    insert_fills(SCOPE, [fill])
    # Query by ET trade date 2024-05-01 must include this row.
    rows = list(fetch_fills(SCOPE, date(2024, 5, 1), date(2024, 5, 1)))
    assert [r["futu_deal_id"] for r in rows] == ["d1"]
