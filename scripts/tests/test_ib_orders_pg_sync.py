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
                "account": "DU0000000",
                "contract": {"secType": "STK", "symbol": "AAPL"},
                "action": "BUY",
                "totalQuantity": 100,
                "limitPrice": 10.5,
            }
        ],
        scope=BROKER_SCOPE,
    )

    assert result == {"registered": 1, "updated": 0, "resurrected": 0, "skipped": 0, "skipped_foreign": 0, "skipped_unprovenanced": 0, "unidentified": 0, "open_count": 1}
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
                "account": "DU0000000",
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
                "account": "DU0000000",
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
                "account": "DU0000000",
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
                "account": "DU0000000",
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
                "account": "DU0000000",
                "contract": {"secType": "STK", "symbol": "QQQ"},
                "action": "BUY",
                "totalQuantity": 50,
                "limitPrice": 500.0,
            }
        ],
        scope=BROKER_SCOPE,
    )
    assert first == {"registered": 1, "updated": 0, "resurrected": 0, "skipped": 0, "skipped_foreign": 0, "skipped_unprovenanced": 0, "unidentified": 0, "open_count": 1}

    second = sync_open_orders_to_postgres(
        [
            {
                "orderId": 7100,
                "permId": 9100,
                "symbol": "QQQ",
                "account": "DU0000000",
                "contract": {"secType": "STK", "symbol": "QQQ"},
                "action": "BUY",
                "totalQuantity": 50,
                "limitPrice": 499.0,  # ← TWS edit
            }
        ],
        scope=BROKER_SCOPE,
    )
    assert second == {"registered": 0, "updated": 1, "resurrected": 0, "skipped": 0, "skipped_foreign": 0, "skipped_unprovenanced": 0, "unidentified": 0, "open_count": 1}


# ---------------------------------------------------------------------------
# Incident #35 — a cancelled order must not come back from ib_async's
# stale openTrades() cache on a long-lived connection.
# ---------------------------------------------------------------------------


class _StaleCacheIB:
    """A long-lived pool connection after ANOTHER client cancelled the order:
    IB's fresh reqAllOpenOrders response omits it, but openTrades() (the
    client-side cache) still holds it as Submitted. Live probe 2026-10-19."""

    def __init__(self, stale_trade):
        self._stale = stale_trade

    def isConnected(self):
        return True

    def reqAllOpenOrders(self):
        return []

    def openTrades(self):
        return [self._stale]

    def sleep(self, _secs):
        return None


def _stale_cache_client(order_id: int, perm_id: int):
    from ib_async import LimitOrder, OrderStatus, Stock, Trade

    from xenon.clients.ib_client import IBClient

    order = LimitOrder("BUY", 5, 200.0, orderId=order_id, permId=perm_id, clientId=12, account="DU0000000")
    trade = Trade(
        contract=Stock("MSFT", "SMART", "USD"),
        order=order,
        orderStatus=OrderStatus(orderId=order_id, status="Submitted"),
    )
    client = IBClient()
    client._ib = _StaleCacheIB(trade)
    return client


def _state(perm_id: str):
    engine = get_sync_engine()
    with engine.connect() as conn:
        return conn.execute(
            select(order_submissions.c.state).where(order_submissions.c.perm_id == perm_id)
        ).scalar_one()


def test_cancelled_snapshot_row_not_resurrected_from_stale_open_trades_cache():
    from datetime import datetime, timezone

    from sqlalchemy import update

    from xenon.execution.ib_orders import fetch_open_orders, sync_open_orders_to_postgres

    sync_open_orders_to_postgres(
        [
            {
                "orderId": 8301,
                "permId": 8311,
                "symbol": "MSFT",
                "account": "DU0000000",
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
            .where(order_submissions.c.perm_id == "8311")
            .values(state="CANCELLED", reason_code="USER_CANCEL")
        )

    open_orders = fetch_open_orders(_stale_cache_client(8301, 8311))
    result = sync_open_orders_to_postgres(
        open_orders, scope=BROKER_SCOPE, observed_at=datetime.now(timezone.utc)
    )

    assert result["resurrected"] == 0
    assert _state("8311") == "CANCELLED"
    assert open_orders == []


def test_cancelled_uuid_row_not_resurrected_from_stale_open_trades_cache():
    """The #192 UUID resurrect path: a Xenon-placed order cancelled by the
    cancel subprocess must stay CANCELLED on the pool's next mirror tick."""
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    from sqlalchemy import insert

    from xenon.execution.ib_orders import fetch_open_orders, sync_open_orders_to_postgres

    now = datetime.now(timezone.utc)
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id="5f0c2f1e-0000-4000-8000-000000008401",
                user_id="u",
                client_attempt_id="ca-8401",
                state="CANCELLED",
                reason_code="USER_CANCEL",
                ticker="MSFT",
                security_type="STK",
                action="BUY",
                quantity=5,
                limit_price=Decimal("200.00"),
                tif="DAY",
                perm_id="8411",
                ib_order_id="8401",
                placing_client_id=12,
                submitted_at=now - timedelta(minutes=5),
                updated_at=now - timedelta(minutes=1),
                modify_sequence=0,
                **BROKER_SCOPE,
            )
        )

    open_orders = fetch_open_orders(_stale_cache_client(8401, 8411))
    result = sync_open_orders_to_postgres(open_orders, scope=BROKER_SCOPE, observed_at=now)

    assert result["resurrected"] == 0
    assert _state("8411") == "CANCELLED"
    assert open_orders == []
