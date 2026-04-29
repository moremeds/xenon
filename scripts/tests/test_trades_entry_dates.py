from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import insert

from xenon.api.server import app
from xenon.db.engine import get_sync_engine
from xenon.db.schema import trades


def _insert_trade(
    *,
    ticker: str,
    opened_at: datetime,
    account_env: str = "paper",
    broker_account: str = "DU0000000",
) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(trades).values(
                ticker=ticker,
                action="BUY",
                quantity=1,
                structure="Stock",
                entry_cost=Decimal("100.00"),
                opened_at=opened_at,
                broker="IB",
                account_env=account_env,
                broker_account=broker_account,
                state="OPEN",
            )
        )


def test_trades_entry_dates_returns_earliest_open_by_scope():
    app.state.broker = "IB"
    app.state.trading_mode = "paper"
    app.state.account = "DU0000000"
    app.state.mode_verified = True

    _insert_trade(ticker="AAPL", opened_at=datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc))
    _insert_trade(ticker="AAPL", opened_at=datetime(2026, 3, 1, 14, 0, tzinfo=timezone.utc))
    _insert_trade(ticker="MSFT", opened_at=datetime(2026, 3, 5, 14, 0, tzinfo=timezone.utc))
    _insert_trade(
        ticker="AAPL",
        opened_at=datetime(2026, 2, 1, 14, 0, tzinfo=timezone.utc),
        account_env="live",
        broker_account="U0000000",
    )

    response = TestClient(app).get("/trades/entry-dates")

    assert response.status_code == 200
    assert response.json() == {
        "AAPL": "2026-03-01T14:00:00+00:00",
        "MSFT": "2026-03-05T14:00:00+00:00",
    }
