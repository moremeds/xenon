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

    assert result == {"registered": 1, "updated": 0, "resurrected": 0, "skipped": 0, "open_count": 1}
    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(select(order_submissions)).one()._mapping
    assert row["perm_id"] == "9001"
    assert row["ib_order_id"] == "7001"
    assert row["ticker"] == "AAPL"
    assert row["broker_account"] == "DU0000000"


def test_sync_registers_bag_order_with_zero_order_id():
    """IB returns BAG combo orders with orderId=0 when the order originated
    from a different clientId than the one fetching open orders. The poller
    must not silently drop them — the perm_id is sufficient to identify the
    snapshot row.
    """
    from xenon.execution.ib_orders import sync_open_orders_to_postgres

    result = sync_open_orders_to_postgres(
        [
            {
                "orderId": 0,
                "permId": 272529181,
                "symbol": "SPX Spread",
                "contract": {"secType": "BAG", "symbol": "SPX", "conId": 28812380},
                "action": "SELL",
                "totalQuantity": 4,
                "limitPrice": 3.9,
                "tif": "GTC",
            }
        ],
        scope=BROKER_SCOPE,
    )

    assert result["registered"] == 1
    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(select(order_submissions).where(order_submissions.c.perm_id == "272529181")).one()._mapping
    assert row["security_type"] == "BAG"
    assert row["ticker"] == "SPX"
    assert row["state"] == "WORKING"


def test_sync_persists_tif_from_ib_order():
    """A GTC order in TWS must round-trip through register_from_snapshot
    so the /orders panel renders 'GTC' instead of always 'DAY'.
    """
    from xenon.execution.ib_orders import sync_open_orders_to_postgres

    sync_open_orders_to_postgres(
        [
            {
                "orderId": 16,
                "permId": 9999,
                "symbol": "QQQ",
                "contract": {"secType": "STK", "symbol": "QQQ"},
                "action": "BUY",
                "totalQuantity": 1,
                "limitPrice": 630.96,
                "tif": "GTC",
            }
        ],
        scope=BROKER_SCOPE,
    )

    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(select(order_submissions).where(order_submissions.c.perm_id == "9999")).one()._mapping
    assert row["tif"] == "GTC"


def test_sync_resurrects_cancelled_snapshot_when_ib_still_reports_open():
    """If a snapshot row is locally CANCELLED but IB still reports it open,
    the next sync must restore it to WORKING. Otherwise a misclassification
    (operator error, race in the cancel route, etc.) leaves the order
    invisible in the panel forever.
    """
    from sqlalchemy import update

    from xenon.execution.ib_orders import sync_open_orders_to_postgres

    sync_open_orders_to_postgres(
        [
            {
                "orderId": 8001,
                "permId": 8101,
                "symbol": "MSFT",
                "contract": {"secType": "STK", "symbol": "MSFT"},
                "action": "BUY",
                "totalQuantity": 5,
                "limitPrice": 200.0,
            }
        ],
        scope=BROKER_SCOPE,
    )

    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            update(order_submissions)
            .where(order_submissions.c.perm_id == "8101")
            .values(state="CANCELLED", reason_code="USER_CANCEL")
        )

    result = sync_open_orders_to_postgres(
        [
            {
                "orderId": 8001,
                "permId": 8101,
                "symbol": "MSFT",
                "contract": {"secType": "STK", "symbol": "MSFT"},
                "action": "BUY",
                "totalQuantity": 5,
                "limitPrice": 200.0,
            }
        ],
        scope=BROKER_SCOPE,
    )

    with engine.connect() as conn:
        row = conn.execute(select(order_submissions).where(order_submissions.c.perm_id == "8101")).one()._mapping
    assert row["state"] == "WORKING"
    assert row["reason_code"] is None
    assert result["resurrected"] == 1


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
    assert first == {"registered": 1, "updated": 0, "resurrected": 0, "skipped": 0, "open_count": 1}

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
    assert second == {"registered": 0, "updated": 1, "resurrected": 0, "skipped": 0, "open_count": 1}
