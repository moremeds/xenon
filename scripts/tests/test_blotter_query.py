"""W3 — Postgres-backed blotter query shape."""

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import insert

from xenon.db.engine import get_sync_engine
from xenon.db.schema import trades
from xenon.execution.account_scope import AccountScope


BROKER_SCOPE = {
    "broker": "IB",
    "account_env": "paper",
    "broker_account": "DU0000000",
}


def _insert_trade(**values) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(insert(trades).values(**values))


def test_fetch_blotter_pg_returns_scoped_closed_and_open_trades():
    from xenon.db.queries.blotter import fetch_blotter_pg

    opened_at = datetime(2026, 4, 28, 14, 30, tzinfo=timezone.utc)
    closed_at = datetime(2026, 4, 28, 15, 30, tzinfo=timezone.utc)

    _insert_trade(
        ticker="AAPL",
        structure="Stock",
        action="BUY",
        quantity=100,
        entry_cost=Decimal("1000.0000"),
        exit_cost=Decimal("1240.0000"),
        realized_pnl=Decimal("240.00"),
        opened_at=opened_at,
        closed_at=closed_at,
        state="CLOSED",
        metadata={
            "contract_desc": "AAPL Stock",
            "sec_type": "STK",
            "legs": [
                {
                    "exec_id": "exec-aapl-open",
                    "side": "BUY",
                    "qty": 100,
                    "price": "10.00",
                    "commission": "1.25",
                    "filled_at": opened_at.isoformat(),
                },
                {
                    "exec_id": "exec-aapl-close",
                    "side": "SELL",
                    "qty": 100,
                    "price": "12.40",
                    "commission": "1.25",
                    "filled_at": closed_at.isoformat(),
                },
            ],
        },
        **BROKER_SCOPE,
    )
    _insert_trade(
        ticker="MSFT",
        structure="Stock",
        action="BUY",
        quantity=50,
        entry_cost=Decimal("500.0000"),
        opened_at=opened_at,
        state="OPEN",
        metadata={
            "legs": [
                {
                    "exec_id": "exec-msft-open",
                    "side": "BUY",
                    "qty": 50,
                    "price": "10.00",
                    "commission": "0.75",
                    "filled_at": opened_at.isoformat(),
                }
            ]
        },
        **BROKER_SCOPE,
    )
    _insert_trade(
        ticker="TSLA",
        structure="Stock",
        action="BUY",
        quantity=10,
        entry_cost=Decimal("100.0000"),
        opened_at=opened_at,
        state="OPEN",
        metadata={"legs": []},
        broker="IB",
        account_env="paper",
        broker_account="DU9999999",
    )

    engine = get_sync_engine()
    scope = AccountScope(broker="IB", account_env="paper", broker_account="DU0000000")
    with engine.connect() as conn:
        payload = fetch_blotter_pg(conn, scope=scope, days=30)

    assert payload["configured"] is True
    assert payload["source"] == "postgres"
    assert payload["summary"] == {
        "closed_trades": 1,
        "open_trades": 1,
        "total_commissions": 3.25,
        "realized_pnl": 240.0,
    }
    assert payload["closed_trades"][0]["symbol"] == "AAPL"
    assert payload["closed_trades"][0]["contract_desc"] == "AAPL Stock"
    assert payload["closed_trades"][0]["is_closed"] is True
    assert payload["closed_trades"][0]["executions"][-1]["exec_id"] == "exec-aapl-close"
    assert payload["open_trades"][0]["symbol"] == "MSFT"
    assert payload["open_trades"][0]["is_closed"] is False
