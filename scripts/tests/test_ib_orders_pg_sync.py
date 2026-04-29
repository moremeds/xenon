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

    assert result == {"registered": 1, "updated": 0, "skipped": 0, "open_count": 1}
    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(select(order_submissions)).one()._mapping
    assert row["perm_id"] == "9001"
    assert row["ib_order_id"] == "7001"
    assert row["ticker"] == "AAPL"
    assert row["broker_account"] == "DU0000000"


def test_sync_counts_updates_and_skips_separately():
    """A second sync with drifted price should count as `updated`, not `registered`."""
    from xenon.execution.ib_orders import sync_open_orders_to_postgres

    first = sync_open_orders_to_postgres(
        [
            {
                "orderId": 7100,
                "permId": 9100,
                "symbol": "QQQ",
                "contract": {"secType": "STK", "symbol": "QQQ"},
                "action": "BUY",
                "totalQuantity": 50,
                "limitPrice": 500.0,
            }
        ],
        scope=BROKER_SCOPE,
    )
    assert first == {"registered": 1, "updated": 0, "skipped": 0, "open_count": 1}

    second = sync_open_orders_to_postgres(
        [
            {
                "orderId": 7100,
                "permId": 9100,
                "symbol": "QQQ",
                "contract": {"secType": "STK", "symbol": "QQQ"},
                "action": "BUY",
                "totalQuantity": 50,
                "limitPrice": 499.0,  # ← TWS edit
            }
        ],
        scope=BROKER_SCOPE,
    )
    assert second == {"registered": 0, "updated": 1, "skipped": 0, "open_count": 1}
