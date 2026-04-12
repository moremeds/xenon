"""Tests for /uw-stats FastAPI endpoints.

Covers GET /uw-stats, POST /uw-stats/reset, GET /uw-stats/history,
and POST /uw-stats/history/clear.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def mock_stats():
    """Mock the process-wide stats singleton."""
    stats = MagicMock()
    stats.get_stats.return_value = {
        "totals": {"requests": 100, "success": 90, "cached": 30},
        "latency_ms": {"samples": 90, "avg": 200, "p95": 500},
        "uptime_seconds": 3600,
    }
    stats.get_hourly_history.return_value = [
        {"hour": "2026-04-08T14:00:00Z", "requests_2xx": 50},
        {"hour": "2026-04-08T15:00:00Z", "requests_2xx": 60},
    ]
    return stats


@pytest.fixture
def client(mock_stats):
    """TestClient with isolated FastAPI app (avoids importing full server).

    Patches at the source module (utils.uw_api_stats.stats) because
    uw_stats routes use lazy imports inside each function body —
    there's no module-level stats attribute to patch on the route.
    """
    with patch("utils.uw_api_stats.stats", mock_stats):
        from api.routes.uw_stats import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)
        yield TestClient(app)


class TestGetUwStats:
    def test_returns_stats_snapshot(self, client, mock_stats):
        r = client.get("/uw-stats")
        assert r.status_code == 200
        body = r.json()
        assert body["totals"]["requests"] == 100
        assert body["latency_ms"]["p95"] == 500
        mock_stats.get_stats.assert_called_once()

    def test_returns_dict(self, client):
        r = client.get("/uw-stats")
        assert isinstance(r.json(), dict)


class TestPostUwStatsReset:
    def test_resets_session_counters(self, client, mock_stats):
        r = client.post("/uw-stats/reset")
        assert r.status_code == 200
        assert r.json()["status"] == "reset"
        mock_stats.reset.assert_called_once()


class TestGetUwStatsHistory:
    def test_returns_hourly_buckets(self, client, mock_stats):
        r = client.get("/uw-stats/history")
        assert r.status_code == 200
        body = r.json()
        assert "buckets" in body
        assert len(body["buckets"]) == 2
        mock_stats.get_hourly_history.assert_called_once_with(hours=96)

    def test_custom_hours_parameter(self, client, mock_stats):
        r = client.get("/uw-stats/history?hours=24")
        assert r.status_code == 200
        mock_stats.get_hourly_history.assert_called_once_with(hours=24)

    def test_rejects_hours_below_minimum(self, client):
        r = client.get("/uw-stats/history?hours=0")
        assert r.status_code == 422  # FastAPI validation error

    def test_rejects_hours_above_maximum(self, client):
        r = client.get("/uw-stats/history?hours=200")
        assert r.status_code == 422


class TestPostUwStatsHistoryClear:
    def test_clears_all_history(self, client, mock_stats):
        r = client.post("/uw-stats/history/clear")
        assert r.status_code == 200
        assert r.json()["status"] == "cleared"
        mock_stats.clear_history.assert_called_once()
