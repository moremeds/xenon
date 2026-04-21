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
    # server.test_mode is captured at module import — force it on in case the
    # server module was imported before this test (e.g. by another test file
    # earlier in the session) without the env var set. Prevents CI flakiness
    # from test-ordering changes.
    import xenon.api.server as server_module

    monkeypatch.setattr(server_module, "test_mode", True, raising=False)
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
    # xenonApi.ts reads body.detail for human-readable error copy. Without it
    # xenonFetch falls through to JSON.stringify(body), which surfaces as an
    # unreadable blob in the UI.
    assert "detail" in body and body["detail"], (
        f"block response must include human-readable `detail` for xenonFetch; got {body}"
    )


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


def test_missing_portfolio_fails_open(monkeypatch, tmp_path):
    """Codex P1 #1: TS guard at web/app/api/orders/place/route.ts:183-185 fails
    OPEN when portfolio.json is absent (logs + skips enforcement). Preflight
    must match that behavior — otherwise a fresh server start blocks every SELL.
    """
    # Override the autouse fixture's data dir with one that has NO portfolio file.
    empty_dir = tmp_path / "nodata"
    empty_dir.mkdir()
    monkeypatch.setenv("XENON_DATA_DIR", str(empty_dir))

    from xenon.api.server import app

    client = TestClient(app)
    # A SELL that would be blocked as INSUFFICIENT_SHARES with an empty portfolio
    # should pass preflight when the portfolio file is missing (fail-open).
    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "SPY",
            "action": "SELL",
            "quantity": 100,
            "limitPrice": 500.0,
        },
    )
    assert resp.status_code != 400 or "reason_code" not in resp.json(), (
        f"Missing portfolio.json must fail open (match TS guard); got {resp.status_code} {resp.json()}"
    )
