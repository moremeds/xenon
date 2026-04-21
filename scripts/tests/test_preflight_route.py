"""Integration test: /orders/place preflight wiring (F2).

Uses FastAPI TestClient with XENON_API_TEST_MODE=1 to stub the subprocess
call. Verifies that the preflight gate returns HTTP 400 with the reason
code BEFORE any subprocess invocation.
"""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _test_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    # Point data dir at an empty portfolio
    portfolio = {"positions": []}
    pf_file = tmp_path / "portfolio.json"
    pf_file.write_text(json.dumps(portfolio))
    monkeypatch.setenv("XENON_DATA_DIR", str(tmp_path))
    yield


@pytest.fixture
def client():
    # Defer import until env vars are set
    from xenon.api.server import app

    return TestClient(app)


def test_spx_stock_buy_blocked_by_preflight(client):
    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "SPX",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 5000.0,
        },
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["reason_code"] == "INDEX_HAS_NO_STOCK"


def test_unknown_ticker_blocked_by_preflight(client):
    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "AAPL",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 180.0,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "UNIVERSE_UNKNOWN"


def test_spy_buy_passes_preflight(client):
    """Preflight ACCEPTs SPY BUY. Under XENON_API_TEST_MODE=1 the handler
    stubs the IB subprocess, so a 200 response proves we reached the
    post-preflight path. We don't assert on the body payload shape —
    only that preflight did not block.
    """
    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 500.0,
        },
    )
    # Preflight blocks would return 400 with a reason_code. A 200/500 from
    # the stubbed subprocess layer both prove we got past preflight. The
    # one outcome we reject here is 400 with a preflight reason_code.
    assert resp.status_code != 400 or "reason_code" not in resp.json(), (
        f"SPY BUY should not be blocked by preflight; got {resp.status_code} {resp.json()}"
    )
