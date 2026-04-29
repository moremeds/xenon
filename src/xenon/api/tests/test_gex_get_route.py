"""Tests for GET /gex — Postgres read path for GEX levels."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert


@pytest.fixture
def client():
    from xenon.api.server import app

    return TestClient(app)


def _seed(payload: dict) -> None:
    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import gex_snapshots

    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(gex_snapshots).values(
                ticker=payload.get("ticker", "SPX"),
                data_date=date.fromisoformat(payload.get("data_date", "2026-04-28")),
                payload=payload,
            )
        )


def test_gex_returns_empty_shape_when_no_snapshots(client):
    resp = client.get("/gex")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scan_time"] == ""
    assert body["ticker"] == "SPX"
    assert body["profile"] == []
    assert "market_open" in body


def test_gex_returns_latest_payload_for_ticker(client):
    _seed({"scan_time": "2026-04-28T14:00:00Z", "ticker": "SPX", "data_date": "2026-04-28", "net_gex": 1})
    _seed({"scan_time": "2026-04-28T15:00:00Z", "ticker": "SPX", "data_date": "2026-04-28", "net_gex": 2})
    _seed({"scan_time": "2026-04-28T15:30:00Z", "ticker": "NDX", "data_date": "2026-04-28", "net_gex": 99})

    resp = client.get("/gex?ticker=SPX")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticker"] == "SPX"
    assert body["net_gex"] == 2
