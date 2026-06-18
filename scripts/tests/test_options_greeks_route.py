"""GET /options/greeks route — thin subprocess dispatcher (mirrors /market-depth).

Verifies arg forwarding (symbol + option triplet upper-cased, --port), the
required-triplet / invalid-right 422 guards (no subprocess spawned), and the
502 mapping for subprocess failure — without a live IB.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import xenon.api.server as server_mod
from xenon.api.subprocess import ScriptResult
from xenon.clients.ib_client import DEFAULT_GATEWAY_PORT


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, dict]:
    captured: dict = {}

    async def fake_recovery(entry: str, args: list, timeout: float = 30) -> ScriptResult:
        captured["entry"] = entry
        captured["args"] = list(args)
        return ScriptResult(
            ok=True,
            data={
                "symbol": "QQQ",
                "conId": 770000001,
                "secType": "OPT",
                "expiry": "20260717",
                "strike": 600.0,
                "right": "C",
                "asOf": "2026-06-18T14:00:00+00:00",
                "bid": 12.3,
                "ask": 12.7,
                "greeks": {
                    "impliedVol": 0.21,
                    "delta": 0.54,
                    "gamma": 0.01,
                    "vega": 0.45,
                    "theta": -0.12,
                    "undPrice": 601.5,
                },
            },
        )

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_recovery)
    return TestClient(server_mod.app), captured


def test_forwards_symbol_triplet_port(client):
    tc, captured = client
    resp = tc.get(
        "/options/greeks",
        params={"symbol": "qqq", "expiry": "20260717", "strike": 600, "right": "c"},
    )
    assert resp.status_code == 200, resp.text
    assert captured["entry"] == "xenon-ib-option-greeks"
    a = captured["args"]
    assert "QQQ" in a  # symbol upper-cased
    assert "--expiry" in a and "20260717" in a
    assert "--strike" in a and "600.0" in a
    assert "--right" in a and "C" in a  # upper-cased
    assert "--port" in a and str(DEFAULT_GATEWAY_PORT) in a
    assert resp.json()["greeks"]["delta"] == 0.54


def test_missing_triplet_is_422_no_subprocess(client):
    tc, captured = client
    # strike + right but no expiry — FastAPI rejects the missing required param
    resp = tc.get("/options/greeks", params={"symbol": "QQQ", "strike": 600, "right": "C"})
    assert resp.status_code == 422, resp.text
    assert captured == {}  # never dispatched


def test_invalid_right_is_422_no_subprocess(client):
    tc, captured = client
    resp = tc.get(
        "/options/greeks",
        params={"symbol": "QQQ", "expiry": "20260717", "strike": 600, "right": "X"},
    )
    assert resp.status_code == 422, resp.text
    assert captured == {}  # rejected before any subprocess


def test_blank_right_is_422_no_subprocess(client):
    tc, captured = client
    resp = tc.get(
        "/options/greeks",
        params={"symbol": "QQQ", "expiry": "20260717", "strike": 600, "right": ""},
    )
    assert resp.status_code == 422, resp.text
    assert captured == {}


def test_greeks_null_still_200(monkeypatch):
    async def no_greeks(entry, args, timeout=30):
        return ScriptResult(
            ok=True,
            data={
                "symbol": "QQQ",
                "conId": 770000001,
                "secType": "OPT",
                "expiry": "20260717",
                "strike": 600.0,
                "right": "C",
                "asOf": "2026-06-18T14:00:00+00:00",
                "bid": None,
                "ask": None,
                "greeks": None,
                "note": "no greeks returned",
            },
        )

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", no_greeks)
    tc = TestClient(server_mod.app)
    resp = tc.get(
        "/options/greeks",
        params={"symbol": "QQQ", "expiry": "20260717", "strike": 600, "right": "C"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["greeks"] is None
    assert body["note"] == "no greeks returned"


def test_subprocess_failure_maps_to_502(monkeypatch):
    async def fail(entry, args, timeout=30):
        return ScriptResult(ok=False, error="Connect call failed (127.0.0.1, 4001)")

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fail)
    tc = TestClient(server_mod.app)
    resp = tc.get(
        "/options/greeks",
        params={"symbol": "QQQ", "expiry": "20260717", "strike": 600, "right": "C"},
    )
    assert resp.status_code == 502, resp.text


def test_cli_error_payload_maps_to_502(monkeypatch):
    async def err(entry, args, timeout=30):
        return ScriptResult(ok=True, data={"error": "could not qualify ZZZZ 20260717 600C"})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", err)
    tc = TestClient(server_mod.app)
    resp = tc.get(
        "/options/greeks",
        params={"symbol": "ZZZZ", "expiry": "20260717", "strike": 600, "right": "C"},
    )
    assert resp.status_code == 502, resp.text
