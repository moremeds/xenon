"""GET /market-depth route — thin subprocess dispatcher (mirrors /options/chain).

Verifies arg forwarding (symbol upper-cased, --port, --num-rows, option triplet),
the all-or-none 422 guard (no subprocess spawned on a partial tuple), and the
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
                "conId": 320227571,
                "secType": "STK",
                "isSmartDepth": True,
                "entitled": True,
                "numRows": 10,
                "asOf": "2026-06-18T14:00:00+00:00",
                "bids": [{"price": 500.1, "size": 3, "marketMaker": "NSDQ"}],
                "asks": [{"price": 500.2, "size": 2, "marketMaker": "ARCA"}],
            },
        )

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fake_recovery)
    return TestClient(server_mod.app), captured


def test_forwards_symbol_port_rows(client):
    tc, captured = client
    resp = tc.get("/market-depth", params={"symbol": "qqq", "num_rows": 5})
    assert resp.status_code == 200, resp.text
    assert captured["entry"] == "xenon-ib-market-depth"
    assert "--port" in captured["args"] and str(DEFAULT_GATEWAY_PORT) in captured["args"]
    assert "--num-rows" in captured["args"] and "5" in captured["args"]
    assert "QQQ" in captured["args"]  # upper-cased
    assert resp.json()["conId"] == 320227571


def test_forwards_option_triplet(client):
    tc, captured = client
    resp = tc.get(
        "/market-depth",
        params={
            "symbol": "QQQ",
            "expiry": "20260618",
            "strike": 500,
            "right": "c",
        },
    )
    assert resp.status_code == 200, resp.text
    a = captured["args"]
    assert "--expiry" in a and "20260618" in a
    assert "--strike" in a and "500.0" in a
    assert "--right" in a and "C" in a  # upper-cased


def test_partial_option_tuple_is_422(client):
    tc, captured = client
    # expiry + strike but no right -> 422, subprocess never invoked
    resp = tc.get(
        "/market-depth",
        params={
            "symbol": "QQQ",
            "expiry": "20260618",
            "strike": 500,
        },
    )
    assert resp.status_code == 422, resp.text
    assert captured == {}  # no subprocess call


def test_num_rows_out_of_range_is_422(client):
    tc, _ = client
    assert tc.get("/market-depth", params={"symbol": "QQQ", "num_rows": 999}).status_code == 422
    assert tc.get("/market-depth", params={"symbol": "QQQ", "num_rows": 0}).status_code == 422


def test_subprocess_failure_maps_to_502(monkeypatch):
    async def fail(entry, args, timeout=30):
        return ScriptResult(ok=False, error="Connect call failed (127.0.0.1, 4001)")

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", fail)
    tc = TestClient(server_mod.app)
    resp = tc.get("/market-depth", params={"symbol": "QQQ"})
    assert resp.status_code == 502, resp.text


def test_cli_error_payload_maps_to_502(monkeypatch):
    async def err(entry, args, timeout=30):
        return ScriptResult(ok=True, data={"error": "could not qualify ZZZZ"})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", err)
    tc = TestClient(server_mod.app)
    resp = tc.get("/market-depth", params={"symbol": "ZZZZ"})
    assert resp.status_code == 502, resp.text


def test_entitled_false_is_still_200(monkeypatch):
    async def no_l2(entry, args, timeout=30):
        return ScriptResult(
            ok=True,
            data={
                "symbol": "AAPL",
                "conId": 265598,
                "secType": "STK",
                "isSmartDepth": True,
                "entitled": False,
                "numRows": 10,
                "asOf": "2026-06-18T14:00:00+00:00",
                "bids": [],
                "asks": [],
                "note": "no L2 entitlement",
            },
        )

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", no_l2)
    tc = TestClient(server_mod.app)
    resp = tc.get("/market-depth", params={"symbol": "AAPL"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["entitled"] is False
    assert body["note"] == "no L2 entitlement"
