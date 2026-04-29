"""Tests for PG-backed scanner/discover/regime read routes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert


@pytest.fixture
def client():
    from xenon.api.server import app

    return TestClient(app)


def _seed(scan_type: str, payload: dict) -> None:
    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import scan_results

    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(insert(scan_results).values(scan_type=scan_type, payload=payload))


def test_scan_get_returns_latest_pg_payload(client):
    _seed("watchlist", {"scan_id": "old", "candidates": []})
    _seed("watchlist", {"scan_id": "new", "candidates": [{"ticker": "AAPL"}]})

    body = client.get("/scan").json()
    assert body["scan_id"] == "new"
    assert body["candidates"][0]["ticker"] == "AAPL"
    assert "_scanned_at" in body


def test_discover_get_returns_latest_pg_payload(client):
    _seed("discover", {"discovery_time": "old", "candidates_found": 0, "candidates": []})
    _seed("discover", {"discovery_time": "new", "candidates_found": 1, "candidates": [{"ticker": "MSFT"}]})

    body = client.get("/discover").json()
    assert body["discovery_time"] == "new"
    assert body["candidates_found"] == 1


def test_regime_get_returns_latest_cri_payload(client):
    _seed("cri", {"scan_time": "old", "date": "2026-04-27", "cri": {"score": 1}, "history": []})
    _seed("cri", {"scan_time": "new", "date": "2026-04-28", "cri": {"score": 2}, "history": []})

    body = client.get("/regime").json()
    assert body["scan_time"] == "new"
    assert body["cri"]["score"] == 2
    assert isinstance(body["market_open"], bool)
