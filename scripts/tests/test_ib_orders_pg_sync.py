"""W5.3 — ib_orders --sync writes open-order snapshots to Postgres."""

from sqlalchemy import select

from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_submissions


BROKER_SCOPE = {
    "broker": "IB",
    "account_env": "paper",
    "broker_account": "DU0000000",
}


def test_sync_open_orders_to_postgres_registers_snapshot_rows():
    from xenon.execution.ib_orders import sync_open_orders_to_postgres

    result = sync_open_orders_to_postgres(
        [
            {
                "orderId": 7001,
                "permId": 9001,
                "symbol": "AAPL",
                "contract": {"secType": "STK", "symbol": "AAPL"},
                "action": "BUY",
                "totalQuantity": 100,
                "limitPrice": 10.5,
            }
        ],
        scope=BROKER_SCOPE,
    )

    assert result == {"registered": 1, "open_count": 1}
    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(select(order_submissions)).one()._mapping
    assert row["perm_id"] == "9001"
    assert row["ib_order_id"] == "7001"
    assert row["ticker"] == "AAPL"
    assert row["broker_account"] == "DU0000000"
