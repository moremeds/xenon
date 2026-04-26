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
    # Preflight blocks would return 400 with a preflight reason_code.
    # A 200/500 means we reached the post-preflight path. A 400 with a
    # non-preflight reason (e.g. STALE_QUOTE from F3) also means preflight
    # ACCEPTed — we only reject preflight-origin codes here.
    preflight_codes = {
        "UNIVERSE_UNKNOWN",
        "INDEX_HAS_NO_STOCK",
        "INSUFFICIENT_SHARES",
        "INSUFFICIENT_CASH",
        "INDEX_CALL_UNCOVERED",
        "ETF_CALL_UNCOVERED",
    }
    body_json = resp.json() if resp.status_code == 400 else {}
    assert body_json.get("reason_code") not in preflight_codes, (
        f"SPY BUY should not be blocked by preflight; got {resp.status_code} {body_json}"
    )


def test_spoofed_multiplier_cannot_bypass_gate(client, monkeypatch, tmp_path):
    """Codex pass-3 P2: attacker posts multiplier=1 to turn 100 SPY shares into
    100 cover units, bypassing Gate 4 on a SELL call. The server must derive
    multiplier from universe.py, not the request body.
    """
    # Portfolio with 100 SPY shares → 1 contract of cover at multiplier=100
    portfolio = {
        "positions": [
            {
                "ticker": "SPY",
                "structure_type": "Stock",
                "direction": "LONG",
                "contracts": 100,
                "expiry": None,
                "legs": [{"direction": "LONG", "type": "Stock", "contracts": 100, "strike": 0.0}],
            }
        ]
    }
    pf_path = tmp_path / "nodata_multiplier"
    pf_path.mkdir()
    (pf_path / "portfolio.json").write_text(json.dumps(portfolio))
    monkeypatch.setenv("XENON_DATA_DIR", str(pf_path))

    # 2 short calls with spoofed multiplier=1 — would bypass the gate if trusted
    resp = client.post(
        "/orders/place",
        json={
            "type": "option",
            "symbol": "SPY",
            "action": "SELL",
            "quantity": 2,
            "right": "C",
            "expiry": "20260620",
            "strike": 500.0,
            "multiplier": 1,  # malicious value
            "limitPrice": 5.0,
        },
    )
    assert resp.status_code == 400, f"spoofed multiplier=1 must not bypass Gate 4; got {resp.status_code} {resp.json()}"
    assert resp.json()["reason_code"] == "ETF_CALL_UNCOVERED"


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
    preflight_codes = {
        "UNIVERSE_UNKNOWN",
        "INDEX_HAS_NO_STOCK",
        "INSUFFICIENT_SHARES",
        "INSUFFICIENT_CASH",
        "INDEX_CALL_UNCOVERED",
        "ETF_CALL_UNCOVERED",
    }
    body_json = resp.json() if resp.status_code == 400 else {}
    assert body_json.get("reason_code") not in preflight_codes, (
        f"Missing portfolio.json must fail open (match TS guard); got {resp.status_code} {body_json}"
    )


def test_insufficient_shares_when_working_sell_exists(client, monkeypatch, tmp_path):
    """PR-B Step: a pre-existing PENDING SELL row in orders_submissions
    consumes held shares. With held=100 and a PENDING sell of 100 in the
    store, a new SELL 1 SPY must BLOCK with INSUFFICIENT_SHARES.
    """
    import json as _json

    pf = tmp_path / "portfolio.json"
    pf.write_text(
        _json.dumps(
            {
                "positions": [
                    {
                        "ticker": "SPY",
                        "structure_type": "Stock",
                        "direction": "LONG",
                        "contracts": 100,
                        "legs": [{"direction": "LONG", "type": "Stock", "contracts": 100, "strike": 0.0}],
                    }
                ],
                "available_funds": 0,
            }
        )
    )
    monkeypatch.setenv("XENON_DATA_DIR", str(tmp_path))

    from decimal import Decimal

    from xenon.execution import orders_store

    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    orders_store.init_store(db)
    orders_store.reserve_attempt(
        "local",
        "seeded-cid",
        orders_store.RequestRow(
            ticker="SPY",
            security_type="STK",
            action="SELL",
            quantity=100,
            expiry=None,
            strike=None,
            right=None,
            multiplier=100,
            con_id=756733,
            limit_price=Decimal("500.15"),
        ),
        db_path=db,
        # Match the conftest harness scope (paper/DU0000000) so the
        # scope-filtered preflight aggregation finds this seeded reservation.
        broker="IB",
        account_env="paper",
        broker_account="DU0000000",
    )

    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "SPY",
            "action": "SELL",
            "quantity": 1,
            "limitPrice": 500.15,
            "client_attempt_id": "new-cid-1",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "INSUFFICIENT_SHARES"
