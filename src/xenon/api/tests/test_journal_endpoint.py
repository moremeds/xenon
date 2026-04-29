from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from xenon.api.server import app
from xenon.db.engine import get_sync_engine
from xenon.db.schema import journal_entries


def _insert_journal_entry(
    *,
    ticker: str,
    account_env: str = "paper",
    broker_account: str = "DU0000000",
    decision: str = "MANUAL",
    metadata: dict | None = None,
) -> int:
    engine = get_sync_engine()
    with engine.begin() as conn:
        return conn.execute(
            insert(journal_entries)
            .values(
                ticker=ticker,
                decision=decision,
                note="journal note",
                authored_by="test",
                authored_at=datetime(2026, 4, 28, 12, 30, tzinfo=timezone.utc),
                metadata=metadata or {"structure": "Long Call", "entry_cost": 123.45},
                broker="IB",
                account_env=account_env,
                broker_account=broker_account,
            )
            .returning(journal_entries.c.id)
        ).scalar_one()


def test_journal_list_returns_scoped_trade_log_shape():
    entry_id = _insert_journal_entry(ticker="AAPL")
    _insert_journal_entry(ticker="MSFT", broker_account="DU9999999")

    response = TestClient(app).get("/journal")
    body = response.json()

    assert response.status_code == 200
    assert list(body.keys()) == ["trades"]
    assert len(body["trades"]) == 1
    assert body["trades"][0] == {
        "id": entry_id,
        "trade_id": None,
        "date": "2026-04-28",
        "time": "12:30:00",
        "ticker": "AAPL",
        "structure": "Long Call",
        "decision": "MANUAL",
        "notes": "journal note",
        "entry_cost": 123.45,
        "attachments": None,
    }


def test_journal_create_stamps_active_scope():
    response = TestClient(app).post(
        "/journal",
        json={
            "ticker": "NVDA",
            "decision": "POST_MORTEM",
            "note": "risk review",
            "metadata": {"structure": "Put Spread"},
        },
    )
    body = response.json()

    assert response.status_code == 200
    assert body["ticker"] == "NVDA"
    assert body["decision"] == "POST_MORTEM"
    assert body["structure"] == "Put Spread"

    engine = get_sync_engine()
    with engine.connect() as conn:
        row = conn.execute(
            select(journal_entries).where(journal_entries.c.id == body["id"])
        ).one()._mapping
    assert row["broker"] == "IB"
    assert row["account_env"] == "paper"
    assert row["broker_account"] == "DU0000000"


def test_journal_create_requires_ticker_or_trade_id():
    response = TestClient(app).post("/journal", json={"decision": "MANUAL"})
    assert response.status_code == 400
    assert "ticker" in response.json()["detail"]


def test_journal_create_rejects_invalid_trade_id():
    response = TestClient(app).post(
        "/journal",
        json={"trade_id": "not-an-int", "decision": "MANUAL"},
    )

    assert response.status_code == 400
    assert "trade_id" in response.json()["detail"]


def test_journal_create_rejects_invalid_authored_at():
    response = TestClient(app).post(
        "/journal",
        json={"ticker": "NVDA", "authored_at": "not-a-date"},
    )

    assert response.status_code == 400
    assert "authored_at" in response.json()["detail"]
