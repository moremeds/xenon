"""Integration test: /orders/place preflight wiring (F2).

Uses FastAPI TestClient with XENON_API_TEST_MODE=1 to stub the subprocess
call. Verifies that the preflight gate returns HTTP 400 with the reason
code BEFORE any subprocess invocation.

Phase-2 postgres migration: portfolio state is seeded into
xenon.account_snapshots.payload (jsonb) instead of data/portfolio.json.
The autouse `_postgres_orders_test_db` fixture in conftest.py truncates
the table before/after every test, so test bodies just call
`seed_portfolio_snapshot(...)` to install fixtures.
"""

import pytest
from fastapi.testclient import TestClient

from scripts.tests.helpers.portfolio_seed import seed_portfolio_snapshot


@pytest.fixture(autouse=True)
def _test_mode(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    # Seed an empty portfolio snapshot so _load_portfolio_view returns
    # PortfolioView(positions=[]) instead of None — mirrors the previous
    # default of writing {"positions": []} to data/portfolio.json. Tests
    # that need real positions overwrite this with their own seed.
    seed_portfolio_snapshot(
        {"positions": []},
        broker="IB",
        account_env="paper",
        broker_account="DU0000000",
    )
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


def test_spoofed_multiplier_cannot_bypass_gate(client):
    """Codex pass-3 P2: attacker posts multiplier=1 to turn 100 SPY shares into
    100 cover units, bypassing Gate 4 on a SELL call. The server must derive
    multiplier from universe.py, not the request body.
    """
    seed_portfolio_snapshot(
        {
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
        },
        broker="IB",
        account_env="paper",
        broker_account="DU0000000",
    )

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


def test_missing_portfolio_fails_open(client):
    """Codex P1 #1: TS guard at web/app/api/orders/place/route.ts:183-185 fails
    OPEN when the portfolio snapshot is absent (logs + skips enforcement). Preflight
    must match that behavior — otherwise a fresh server start blocks every SELL.

    Clears the autouse empty-portfolio seed so PG truly has no row for this
    scope, forcing _load_portfolio_view to return None.
    """
    from sqlalchemy import text

    from xenon.db.engine import get_sync_engine

    with get_sync_engine().begin() as conn:
        conn.execute(text("TRUNCATE xenon.account_snapshots CASCADE"))

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
        f"Missing portfolio must fail open (match TS guard); got {resp.status_code} {body_json}"
    )


def test_insufficient_shares_when_working_sell_exists(client, monkeypatch, tmp_path):
    """PR-B Step: a pre-existing PENDING SELL row in orders_submissions
    consumes held shares. With held=100 and a PENDING sell of 100 in the
    store, a new SELL 1 SPY must BLOCK with INSUFFICIENT_SHARES.
    """
    seed_portfolio_snapshot(
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
        },
        broker="IB",
        account_env="paper",
        broker_account="DU0000000",
    )

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
