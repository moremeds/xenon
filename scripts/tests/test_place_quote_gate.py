import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

SECRET = "d" * 64


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_QUOTE_TOKEN_SECRET", SECRET)
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    monkeypatch.setenv("XENON_DATA_DIR", str(tmp_path))
    yield


@pytest.fixture
def client():
    from xenon.api import server
    from xenon.execution import orders_store, quote_guard

    orders_store.init_store()
    server._tick_rule_cache = quote_guard.TickRuleCache(
        source=lambda con_id: Decimal("0.01"),
        ttl_seconds=3600,
    )
    return TestClient(server.app)


def _mint_token(con_id=756733, ticker="SPY", age_ms=0, bid="500.10", ask="500.20") -> str:
    from xenon.execution import quote_tokens

    p = quote_tokens.QuotePayload(
        con_id=con_id,
        ticker=ticker,
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=100,
        ask_size=120,
        ts_server_ms=int(time.time() * 1000) - age_ms,
    )
    return quote_tokens.mint(p, SECRET)


def test_missing_quote_token_rejects(client):
    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 500.20,
            "client_attempt_id": "ctx-1",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "STALE_QUOTE"


def test_expired_token_rejects_with_STALE_QUOTE(client):
    token = _mint_token(age_ms=10_000)
    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 500.20,
            "client_attempt_id": "ctx-2",
            "quote_token": token,
            "con_id": 756733,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "STALE_QUOTE"


def test_fresh_token_reaches_idempotency_layer(client):
    token = _mint_token()
    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 500.20,
            "client_attempt_id": "ctx-3",
            "quote_token": token,
            "con_id": 756733,
        },
    )
    assert resp.status_code == 200, resp.text


def test_limit_override_records_PREFLIGHT_ACK_LIMIT_event(client, monkeypatch):
    import os
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import duckdb

    from xenon.api import server

    midday = datetime(2026, 4, 22, 13, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(server, "_now", lambda: midday, raising=False)

    token = _mint_token(bid="9.50", ask="10.00")
    resp = client.post(
        "/orders/place",
        json={
            "type": "option",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 1,
            "right": "C",
            "strike": 500,
            "expiry": "20260620",
            "limitPrice": 12.00,
            "acknowledge_limit_override": True,
            "client_attempt_id": "ack-1",
            "quote_token": token,
            "con_id": 756733,
        },
    )
    assert resp.status_code == 200, resp.text
    con = duckdb.connect(os.environ["XENON_ORDERS_DB_PATH"])
    rows = con.execute("SELECT kind FROM orders_events").fetchall()
    con.close()
    assert ("PREFLIGHT_ACK_LIMIT",) in rows
