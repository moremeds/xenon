from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import insert

from xenon.api.server import app
from xenon.db.engine import get_sync_engine
from xenon.db.schema import order_fills, order_submissions


def _insert_order(
    *,
    submission_id: str,
    ticker: str,
    state: str,
    security_type: str = "STK",
    action: str = "BUY",
    quantity: int = 10,
    con_id: int = 265598,
    ib_order_id: str = "7001",
    perm_id: str = "9001",
    account_env: str = "paper",
    broker_account: str = "DU0000000",
) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_submissions).values(
                submission_id=submission_id,
                user_id="user-1",
                client_attempt_id=submission_id,
                ticker=ticker,
                security_type=security_type,
                action=action,
                quantity=quantity,
                con_id=con_id,
                ib_order_id=ib_order_id,
                perm_id=perm_id,
                limit_price=Decimal("190.50"),
                state=state,
                filled_qty=3,
                avg_fill_price=Decimal("190.25"),
                submitted_at=datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc),
                updated_at=datetime(2026, 3, 10, 14, 5, tzinfo=timezone.utc),
                broker="IB",
                account_env=account_env,
                broker_account=broker_account,
            )
        )


def _insert_fill(
    *,
    exec_id: str,
    ticker: str,
    submission_id: str | None = None,
    perm_id: str = "9002",
    ib_order_id: str = "7002",
    con_id: int = 265598,
    side: str = "BUY",
    qty: int = 2,
    price: str = "191.25",
    commission: str = "1.25",
    metadata: dict | None = None,
    account_env: str = "paper",
    broker_account: str = "DU0000000",
) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_fills).values(
                exec_id=exec_id,
                submission_id=submission_id,
                combo_attempt_id=None,
                perm_id=perm_id,
                ib_order_id=ib_order_id,
                con_id=con_id,
                ticker=ticker,
                side=side,
                qty=qty,
                price=Decimal(price),
                commission=Decimal(commission),
                filled_at=datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc),
                metadata=metadata
                if metadata is not None
                else {"legacy_source": "test", "legacy_id": exec_id, "sec_type": "STK", "exchange": "SMART"},
                broker="IB",
                account_env=account_env,
                broker_account=broker_account,
            )
        )


def test_orders_endpoint_reads_order_submissions_and_fills_by_scope():
    app.state.broker = "IB"
    app.state.trading_mode = "paper"
    app.state.account = "DU0000000"
    app.state.mode_verified = True

    _insert_order(submission_id="open-paper", ticker="AAPL", state="WORKING")
    _insert_order(submission_id="done-paper", ticker="MSFT", state="FILLED")
    _insert_order(submission_id="open-live", ticker="TSLA", state="WORKING", account_env="live", broker_account="U0000000")
    _insert_fill(exec_id="fill-paper", ticker="AAPL")
    _insert_fill(exec_id="fill-live", ticker="TSLA", account_env="live", broker_account="U0000000")

    response = TestClient(app).get("/orders")

    assert response.status_code == 200
    body = response.json()
    assert body["open_count"] == 1
    assert body["executed_count"] == 1
    assert body["open_orders"][0]["symbol"] == "AAPL"
    assert body["open_orders"][0]["orderId"] == 7001
    assert body["open_orders"][0]["remaining"] == 7
    assert body["executed_orders"][0]["execId"] == "fill-paper"
    assert body["executed_orders"][0]["symbol"] == "AAPL"


def test_orders_endpoint_infers_legacy_bag_envelope_and_option_legs_for_snapshot_fills():
    app.state.broker = "IB"
    app.state.trading_mode = "paper"
    app.state.account = "DU0000000"
    app.state.mode_verified = True

    _insert_order(
        submission_id="snapshot-bag",
        ticker="SPX",
        state="FILLED",
        security_type="BAG",
        action="SELL",
        quantity=11,
        con_id=28812380,
        ib_order_id="21",
        perm_id="1564434762",
    )
    _insert_fill(
        exec_id="bag-parent",
        submission_id="snapshot-bag",
        ticker="SPX",
        perm_id="1564434762",
        ib_order_id="21",
        con_id=28812380,
        side="SELL",
        qty=11,
        price="1.40",
        commission="0",
        metadata={"source": "single_leg_rehydrate"},
    )
    _insert_fill(
        exec_id="bag-leg-short",
        submission_id="snapshot-bag",
        ticker="SPX",
        perm_id="1564434762",
        ib_order_id="21",
        con_id=872609959,
        side="SELL",
        qty=11,
        price="27.90",
        commission="14.1950",
        metadata={"source": "single_leg_rehydrate", "realized_pnl": "-29431.38995"},
    )
    _insert_fill(
        exec_id="bag-leg-long",
        submission_id="snapshot-bag",
        ticker="SPX",
        perm_id="1564434762",
        ib_order_id="21",
        con_id=873604441,
        side="BUY",
        qty=11,
        price="26.50",
        commission="14.1950",
        metadata={"source": "single_leg_rehydrate", "realized_pnl": "29154.61005"},
    )

    body = TestClient(app).get("/orders").json()
    by_exec_id = {row["execId"]: row for row in body["executed_orders"]}

    assert by_exec_id["bag-parent"]["contract"]["secType"] == "BAG"
    assert by_exec_id["bag-leg-short"]["contract"]["secType"] == "OPT"
    assert by_exec_id["bag-leg-short"]["realizedPNL"] == -29431.38995
    assert by_exec_id["bag-leg-long"]["contract"]["secType"] == "OPT"
