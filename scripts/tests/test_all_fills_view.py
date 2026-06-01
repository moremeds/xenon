"""xenon.all_fills returns fill-grain rows from both IB and Futu."""

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text

from xenon.db.engine import get_sync_engine
from xenon.db.queries.futu_history import insert_fills
from xenon.execution.account_scope import AccountScope


def test_view_returns_futu_rows(clean_scope):
    insert_fills(
        AccountScope("FUTU", "paper", "pytest"),
        [
            dict(
                futu_deal_id="d1",
                futu_order_id="o",
                ticker="AAPL",
                futu_code="US.AAPL",
                market="US",
                action="BUY",
                quantity=10,
                price=Decimal("150"),
                fees=Decimal("1"),
                filled_at=datetime(2024, 5, 1, 10, tzinfo=timezone.utc),
                raw={},
            )
        ],
    )
    with get_sync_engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT broker, ticker, action, quantity, price, fees
                  FROM xenon.all_fills
                 WHERE broker='FUTU' AND account_env='paper'
                   AND broker_account='pytest' AND ticker='AAPL'
                """
            )
        ).fetchall()
    assert len(rows) == 1
    assert rows[0].action == "BUY"
    assert rows[0].quantity == 10
