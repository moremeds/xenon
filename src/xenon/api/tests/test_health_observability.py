"""W6 health observability for PG migration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert


@pytest.fixture
def client(monkeypatch):
    from xenon.api import server

    async def fake_gateway():
        return {"port_listening": True}

    monkeypatch.setattr(server, "check_ib_gateway", fake_gateway)
    return TestClient(server.app)


def test_health_includes_snapshotter_freshness(client):
    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import account_snapshots

    snapshot_at = datetime.now(timezone.utc) - timedelta(seconds=42)
    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(account_snapshots).values(
                account="DU0000000",
                bankroll=0,
                payload={"positions": []},
                snapshot_at=snapshot_at,
                broker="IB",
                account_env="paper",
                broker_account="DU0000000",
            )
        )

    body = client.get("/health").json()
    assert body["snapshotter"]["last_write_at"].startswith(snapshot_at.date().isoformat())
    assert body["snapshotter"]["stale_seconds"] >= 0


def test_health_includes_recent_unknown_order_alarm(client):
    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import order_submissions

    now = datetime.now(timezone.utc)
    engine = get_sync_engine()
    with engine.begin() as conn:
        for idx in range(6):
            conn.execute(
                insert(order_submissions).values(
                    submission_id=f"unknown-{idx}",
                    user_id="user-1",
                    client_attempt_id=f"attempt-{idx}",
                    ticker="SPY",
                    security_type="STK",
                    action="BUY",
                    quantity=1,
                    multiplier=1,
                    state="UNKNOWN",
                    submitted_at=now,
                    broker="IB",
                    account_env="paper",
                    broker_account="DU0000000",
                )
            )

    body = client.get("/health").json()
    assert body["order_submissions"]["unknown_count"] == 6
    assert body["order_submissions"]["alarm"] is True
