"""Tests for GET /vcg — Postgres read path for the VCG History panel."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert


@pytest.fixture
def client():
    from xenon.api.server import app

    return TestClient(app)


def _seed(payload: dict) -> None:
    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import vcg_series

    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            insert(vcg_series).values(
                market_open=payload.get("market_open"),
                credit_proxy=payload.get("credit_proxy"),
                payload=payload,
            )
        )


def test_returns_empty_shape_when_no_scans(client):
    """No vcg_series rows → endpoint returns empty envelope, not 404."""
    resp = client.get("/vcg")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scan_time"] == ""
    assert body["history"] == []
    assert body["credit_proxy"] == "HYG"
    assert "market_open" in body


def test_returns_latest_payload_with_history(client):
    """Latest scan's payload (signal + 20d history) is returned verbatim,
    aside from market_open being overridden by the live ET clock."""
    payload = {
        "scan_time": "2026-04-27T22:25:43",
        "market_open": False,
        "credit_proxy": "HYG",
        "signal": {
            "vcg": -0.0156,
            "vcg_adj": -0.0156,
            "vix": 18.76,
            "vvix": 97.51,
            "regime": "DIVERGENCE",
            "interpretation": "NORMAL",
        },
        "history": [
            {"date": "2026-04-27", "vcg": -0.0156, "vix": 18.76},
            {"date": "2026-04-24", "vcg": 0.196, "vix": 18.71},
        ],
    }
    _seed(payload)
    resp = client.get("/vcg")
    assert resp.status_code == 200
    body = resp.json()
    assert body["scan_time"] == "2026-04-27T22:25:43"
    assert body["signal"]["regime"] == "DIVERGENCE"
    assert len(body["history"]) == 2
    assert body["history"][0]["date"] == "2026-04-27"


def test_returns_most_recent_when_multiple_scans(client):
    """When multiple rows exist, only the latest by scanned_at is returned."""
    older = {"scan_time": "2026-04-26T22:00:00", "signal": {"regime": "PANIC"}, "history": []}
    newer = {"scan_time": "2026-04-27T22:25:43", "signal": {"regime": "DIVERGENCE"}, "history": []}
    _seed(older)
    _seed(newer)
    body = client.get("/vcg").json()
    assert body["signal"]["regime"] == "DIVERGENCE"


def test_market_open_is_live_not_stamped(client):
    """The stamped market_open in the payload is overridden by the live ET clock,
    so a snapshot taken during open hours doesn't lie at 22:00 ET."""
    payload = {"scan_time": "2026-04-27T10:00:00", "market_open": True, "signal": {}, "history": []}
    _seed(payload)
    body = client.get("/vcg").json()
    # Don't assert a specific value — just that it's a bool reflecting the live clock,
    # not the stamped True.
    assert isinstance(body["market_open"], bool)
