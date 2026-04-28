from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import insert

from xenon.api.server import app
from xenon.db.engine import get_sync_engine
from xenon.db.events import CHANNEL_TRADE_CLOSED
from xenon.db.schema import outbox


def _emit_trade_closed(*, broker_account: str, consumed_by=None) -> None:
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(outbox).values(
                channel=CHANNEL_TRADE_CLOSED,
                source="test",
                payload={
                    "trade_id": 1,
                    "ticker": "AAPL",
                    "broker": "IB",
                    "account_env": "paper",
                    "broker_account": broker_account,
                },
                consumed_by=consumed_by if consumed_by is not None else [],
            )
        )


def test_journal_sync_reports_scoped_pending_trade_closed_outbox():
    _emit_trade_closed(broker_account="DU0000000")
    _emit_trade_closed(broker_account="DU0000000", consumed_by=["journal"])
    _emit_trade_closed(broker_account="DU9999999")

    response = TestClient(app).post("/journal/sync")
    body = response.json()

    assert response.status_code == 200
    assert body == {"imported": 0, "skipped": 0, "pending_outbox": 1}
