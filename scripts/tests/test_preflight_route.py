"""Integration test: /orders/place preflight wiring (F2).

Uses FastAPI TestClient with XENON_API_TEST_MODE=1 to stub the subprocess
call. Verifies that the preflight gate returns HTTP 400 with the reason
code BEFORE any subprocess invocation.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text


def _insert_portfolio_snapshot(payload: dict, *, snapshot_at: datetime | None = None) -> None:
    import os

    engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
    try:
        with engine.begin() as con:
            con.execute(
                text(
                    """
                    INSERT INTO xenon.account_snapshots
                      (account, bankroll, payload, snapshot_at, broker, account_env, broker_account)
                    VALUES
                      (:account, 0, CAST(:payload AS jsonb), :snapshot_at, 'IB', 'paper', 'DU0000000')
                    """
                ),
                {
                    "account": "DU0000000",
                    "payload": json.dumps(payload),
                    "snapshot_at": snapshot_at or datetime.now(timezone.utc),
                },
            )
    finally:
        engine.dispose()


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
    _insert_portfolio_snapshot(portfolio)

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


def test_missing_portfolio_snapshot_blocks_sell(monkeypatch, tmp_path):
    """Runtime preflight must not fall back to data/portfolio.json or fail open
    for SELL exposure. A missing Postgres snapshot is an explicit blocker.
    """
    # Override the autouse fixture's data dir with one that has NO portfolio file.
    empty_dir = tmp_path / "nodata"
    empty_dir.mkdir()
    monkeypatch.setenv("XENON_DATA_DIR", str(empty_dir))

    from xenon.api.server import app

    client = TestClient(app)
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
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "PORTFOLIO_SNAPSHOT_REQUIRED"


def test_preflight_uses_postgres_snapshot_not_portfolio_json(client, monkeypatch, tmp_path):
    """The runtime portfolio source for /orders/place is Postgres. A stale
    data/portfolio.json must not make a covered SELL look naked.
    """
    stale_json_dir = tmp_path / "stale_json"
    stale_json_dir.mkdir()
    (stale_json_dir / "portfolio.json").write_text(json.dumps({"positions": []}))
    monkeypatch.setenv("XENON_DATA_DIR", str(stale_json_dir))
    _insert_portfolio_snapshot(
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
            ],
            "available_funds": 0,
        }
    )

    from xenon.api import server

    async def _fresh_quote(ticker: str, con_id: int):
        return {"bid": 500.10, "ask": 500.20, "bid_size": 100, "ask_size": 120}

    monkeypatch.setattr(server, "_fetch_quote_snapshot", _fresh_quote)

    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "SPY",
            "action": "SELL",
            "quantity": 1,
            "limitPrice": 500.10,
            "con_id": 756733,
            "client_attempt_id": "pg-source-1",
        },
    )
    assert resp.status_code == 200, resp.text


def test_stale_portfolio_snapshot_blocks_sell(client, monkeypatch):
    """SELL exposure must not be approved from a stale PG portfolio snapshot."""
    monkeypatch.setenv("XENON_PORTFOLIO_SNAPSHOT_STALE_S", "300")
    _insert_portfolio_snapshot(
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
            ],
            "available_funds": 0,
        },
        snapshot_at=datetime.now(timezone.utc) - timedelta(minutes=20),
    )

    from xenon.api import server

    monkeypatch.setattr(server, "_is_market_open_now", lambda: True)

    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "SPY",
            "action": "SELL",
            "quantity": 1,
            "limitPrice": 500.10,
            "con_id": 756733,
            "client_attempt_id": "stale-snapshot-1",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "PORTFOLIO_SNAPSHOT_STALE"


def test_closed_market_snapshot_stale_default_is_30_minutes(monkeypatch):
    from xenon.api import server

    monkeypatch.delenv("XENON_PORTFOLIO_SNAPSHOT_STALE_CLOSED_S", raising=False)
    monkeypatch.setattr(server, "_is_market_open_now", lambda: False)
    verdict = server._portfolio_snapshot_stale_response(datetime.now(timezone.utc) - timedelta(minutes=20))
    assert verdict is None


def test_combo_call_ratio_spread_blocked_by_server_preflight(client):
    _insert_portfolio_snapshot({"positions": [], "available_funds": 0})

    resp = client.post(
        "/orders/place",
        json={
            "type": "combo",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 1.00,
            "client_attempt_id": "combo-ratio-1",
            "legs": [
                {"expiry": "20260620", "strike": 500, "right": "C", "action": "BUY", "ratio": 1},
                {"expiry": "20260620", "strike": 510, "right": "C", "action": "SELL", "ratio": 2},
            ],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "ETF_CALL_UNCOVERED"


@pytest.mark.asyncio
async def test_run_preflight_awaits_async_portfolio_loader(monkeypatch):
    from xenon.api import server
    from xenon.execution.preflight import PortfolioView

    called = {"value": False}

    async def _snapshot():
        called["value"] = True
        return PortfolioView(
            positions=[
                {
                    "ticker": "SPY",
                    "structure_type": "Stock",
                    "direction": "LONG",
                    "contracts": 100,
                    "expiry": None,
                    "legs": [{"direction": "LONG", "type": "Stock", "contracts": 100, "strike": 0.0}],
                }
            ],
            available_funds=0,
        )

    monkeypatch.setattr(server, "_load_portfolio_view", _snapshot)
    verdict = await server._run_preflight(
        {
            "type": "stock",
            "symbol": "SPY",
            "action": "SELL",
            "quantity": 1,
            "limitPrice": 500.10,
        }
    )
    assert called["value"] is True
    assert verdict.accept is True


def test_combo_malformed_leg_right_rejects_as_invalid_body(client):
    _insert_portfolio_snapshot({"positions": [], "available_funds": 0})

    resp = client.post(
        "/orders/place",
        json={
            "type": "combo",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 1.00,
            "client_attempt_id": "combo-bad-right-1",
            "legs": [
                {"expiry": "20260620", "strike": 510, "right": "BAD", "action": "SELL", "ratio": 1},
            ],
        },
    )
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "INVALID_ORDER_BODY"


def test_insufficient_shares_when_working_sell_exists(client, monkeypatch, tmp_path):
    """PR-B Step: a pre-existing PENDING SELL row in orders_submissions
    consumes held shares. With held=100 and a PENDING sell of 100 in the
    store, a new SELL 1 SPY must BLOCK with INSUFFICIENT_SHARES.
    """
    import json as _json

    _insert_portfolio_snapshot(
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
