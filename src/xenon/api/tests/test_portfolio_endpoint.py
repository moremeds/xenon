"""Route tests for `GET /portfolio` and `POST /portfolio/sync`.

Phase 1 of the portfolio postgres read-path migration. Verifies:

1. 404 when no snapshot exists for the current scope.
2. 200 with the structured payload when a snapshot exists.
3. Scope isolation: paper rows must not leak into a live response.

The conftest seeds `app.state.{trading_mode, account, mode_verified}` to a
paper default, so tests for live scope override `app.state.account`.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, insert, text

from xenon.db.schema import account_snapshots


def _sync_test_db_url() -> str:
    url = os.environ.get(
        "DATABASE_URL_TEST",
        "postgresql+asyncpg://xenon_app:xenon_dev@localhost:5432/xenon_test",
    )
    return url.replace("postgresql+asyncpg://", "postgresql+psycopg://")


def _seed_snapshot(*, broker: str, account_env: str, broker_account: str, payload: dict, snapshot_at: datetime) -> None:
    engine = create_engine(_sync_test_db_url())
    try:
        with engine.begin() as conn:
            conn.execute(
                insert(account_snapshots).values(
                    account=broker_account,
                    bankroll=0,
                    peak_value=0,
                    net_liquidation=0,
                    payload=payload,
                    snapshot_at=snapshot_at,
                    broker=broker,
                    account_env=account_env,
                    broker_account=broker_account,
                )
            )
    finally:
        engine.dispose()


def _seed_state_for_live():
    """Override the conftest paper default so this test sees a live scope."""
    from xenon.api import server

    server.app.state.trading_mode = "live"
    server.app.state.account = "U18007831"
    server.app.state.mode_verified = True


def test_get_portfolio_returns_404_when_no_snapshot():
    from xenon.api import server

    client = TestClient(server.app)
    res = client.get("/portfolio")
    assert res.status_code == 404
    assert "No portfolio snapshot" in res.json()["detail"]


def test_get_portfolio_returns_payload_when_snapshot_exists():
    from xenon.api import server

    payload = {
        "bankroll": 1000.0,
        "positions": [],
        "last_sync": "2026-04-27T00:00:00Z",
        "account_summary": {"net_liquidation": 1000.0, "daily_pnl": None},
    }
    _seed_snapshot(
        broker="IB",
        account_env="paper",
        broker_account="DU0000000",
        payload=payload,
        snapshot_at=datetime.now(timezone.utc),
    )
    client = TestClient(server.app)
    res = client.get("/portfolio")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["bankroll"] == 1000.0
    assert body["account_summary"]["net_liquidation"] == 1000.0


def test_get_portfolio_scope_isolation_live_does_not_see_paper():
    """The 2026-04-27 bug regression: live session served paper data because
    the prior reader had no scope filter. The new endpoint must reject paper
    rows even when they are newer than the live row.
    """
    _seed_state_for_live()
    now = datetime.now(timezone.utc)

    # Live row (older) — should be returned
    _seed_snapshot(
        broker="IB",
        account_env="live",
        broker_account="U18007831",
        payload={"who": "live", "bankroll": 50000.0, "positions": [], "last_sync": "live-sync"},
        snapshot_at=now - timedelta(minutes=10),
    )
    # Paper row (newer) — must NOT leak
    _seed_snapshot(
        broker="IB",
        account_env="paper",
        broker_account="DU1234567",
        payload={"who": "paper", "bankroll": 100.0, "positions": [], "last_sync": "paper-sync"},
        snapshot_at=now,
    )

    from xenon.api import server

    client = TestClient(server.app)
    res = client.get("/portfolio")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["who"] == "live"
    assert body["bankroll"] == 50000.0
