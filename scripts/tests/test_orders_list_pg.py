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
                security_type="STK",
                action="BUY",
                quantity=10,
                con_id=265598,
                ib_order_id="7001",
                perm_id="9001",
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


def _insert_fill(*, exec_id: str, ticker: str, account_env: str = "paper", broker_account: str = "DU0000000") -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(order_fills).values(
                exec_id=exec_id,
                submission_id=None,
                combo_attempt_id=None,
                perm_id="9002",
                ib_order_id="7002",
                con_id=265598,
                ticker=ticker,
                side="BUY",
                qty=2,
                price=Decimal("191.25"),
                commission=Decimal("1.25"),
                filled_at=datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc),
                metadata={"legacy_source": "test", "legacy_id": exec_id, "sec_type": "STK", "exchange": "SMART"},
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
