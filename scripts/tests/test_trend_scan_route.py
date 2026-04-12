"""Tests for POST /trend-scan FastAPI endpoint.

The route spawns trend_scan.py as a subprocess with 180s timeout.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
from api.subprocess import ScriptResult
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Build isolated app with just the /trend-scan route.

    The route is defined inline in server.py (not a separate router),
    so we replicate it in a fresh FastAPI app to avoid importing the
    full server with IB/Futu/lifespan dependencies.
    """
    from fastapi import FastAPI, HTTPException

    _write_cache_calls = []

    async def _mock_run_script(script, args=None, timeout=30.0):
        # Will be replaced per-test via monkeypatch
        return ScriptResult(ok=True, data={})

    def _mock_write_cache(path, data):
        _write_cache_calls.append((path, data))

    app = FastAPI()
    app._mock_run_script = _mock_run_script
    app._mock_write_cache = _mock_write_cache
    app._write_cache_calls = _write_cache_calls

    @app.post("/trend-scan")
    async def trend_scan():
        result = await app._mock_run_script("trend_scan.py", ["--top", "25"], timeout=180)
        if not result.ok:
            raise HTTPException(status_code=502, detail=result.error)
        app._mock_write_cache("trend_scan.json", result.data)
        return result.data

    return TestClient(app)


class TestPostTrendScan:
    def test_returns_scan_results_on_success(self, client):
        mock_data = {
            "scan_id": "trend_20260410",
            "candidates": [{"ticker": "NVDA", "final_score": 0.82}],
        }
        original = client.app._mock_run_script

        async def _run(script, args=None, timeout=30.0):
            return ScriptResult(ok=True, data=mock_data)

        client.app._mock_run_script = _run
        r = client.post("/trend-scan")
        client.app._mock_run_script = original
        assert r.status_code == 200
        body = r.json()
        assert body["scan_id"] == "trend_20260410"
        assert len(body["candidates"]) == 1

    def test_returns_502_on_script_failure(self, client):
        async def _run(script, args=None, timeout=30.0):
            return ScriptResult(ok=False, error="trend_scan.py crashed")

        client.app._mock_run_script = _run
        r = client.post("/trend-scan")
        assert r.status_code == 502
        assert "crashed" in r.json()["detail"]

    def test_writes_cache_file_on_success(self, client):
        mock_data = {"scan_id": "test"}

        async def _run(script, args=None, timeout=30.0):
            return ScriptResult(ok=True, data=mock_data)

        client.app._mock_run_script = _run
        client.app._write_cache_calls.clear()
        client.post("/trend-scan")
        assert len(client.app._write_cache_calls) == 1
        cache_path, cache_data = client.app._write_cache_calls[0]
        assert "trend_scan.json" in str(cache_path)
        assert cache_data["scan_id"] == "test"
