import time
from decimal import Decimal

import pytest
# Phase 2 carve-out: this module's tests open their own SQLAlchemy engine
# (helpers calling sqlalchemy.create_engine directly, or subprocess CLIs)
# and therefore can't share the test's BEGIN/ROLLBACK transaction. They
# stay on Phase 1 TRUNCATE pre+post isolation via this marker. Migration
# to txn-rollback would require refactoring those local engine helpers to
# go through xenon.db.engine.get_sync_engine().
pytestmark = pytest.mark.committed_db

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

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


def test_missing_quote_token_fetches_fresh_quote_and_accepts(client, monkeypatch):
    from xenon.api import server

    async def _fresh_quote(ticker: str, con_id: int):
        assert ticker == "SPY"
        assert con_id == 756733
        return {
            "bid": Decimal("500.10"),
            "ask": Decimal("500.20"),
            "bid_size": 100,
            "ask_size": 120,
        }

    monkeypatch.setattr(server, "_fetch_quote_snapshot", _fresh_quote)

    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 500.20,
            "con_id": 756733,
            "client_attempt_id": "ctx-1",
        },
    )
    assert resp.status_code == 200, resp.text


def test_expired_token_fetches_fresh_quote_and_accepts(client, monkeypatch):
    from xenon.api import server

    async def _fresh_quote(ticker: str, con_id: int):
        assert ticker == "SPY"
        assert con_id == 756733
        return {
            "bid": Decimal("500.10"),
            "ask": Decimal("500.20"),
            "bid_size": 100,
            "ask_size": 120,
        }

    monkeypatch.setattr(server, "_fetch_quote_snapshot", _fresh_quote)

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
    assert resp.status_code == 200, resp.text


def test_missing_quote_token_without_live_quote_returns_QUOTE_UNAVAILABLE(client, monkeypatch):
    from fastapi import HTTPException

    from xenon.api import server

    async def _no_quote(ticker: str, con_id: int):
        raise HTTPException(status_code=503, detail=f"No quote available for {ticker}/{con_id}")

    monkeypatch.setattr(server, "_fetch_quote_snapshot", _no_quote)

    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 500.20,
            "con_id": 756733,
            "client_attempt_id": "ctx-no-quote",
        },
    )
    assert resp.status_code == 503
    assert resp.json()["reason_code"] == "QUOTE_UNAVAILABLE"


def test_token_contract_mismatch_rejects_without_fallback(client, monkeypatch):
    from xenon.api import server

    async def _unexpected_quote_fetch(ticker: str, con_id: int):
        raise AssertionError("contract mismatch must not fall back to a fresh quote")

    monkeypatch.setattr(server, "_fetch_quote_snapshot", _unexpected_quote_fetch)

    token = _mint_token(con_id=756733, ticker="SPY")
    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "QQQ",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 500.20,
            "client_attempt_id": "ctx-mismatch",
            "quote_token": token,
            "con_id": 320227571,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "QUOTE_CONTRACT_MISMATCH"


def test_option_token_contract_mismatch_without_body_con_id_rejects(client, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from xenon.api import server

    midday = datetime(2026, 4, 22, 13, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(server, "_now", lambda: midday, raising=False)

    async def _qualified_con_id(body: dict):
        assert body["type"] == "option"
        assert body["symbol"] == "SPY"
        return 222222

    monkeypatch.setattr(server, "_qualify_order_con_id", _qualified_con_id, raising=False)

    token = _mint_token(con_id=111111, ticker="SPY", bid="4.90", ask="5.00")
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
            "limitPrice": 5.00,
            "client_attempt_id": "ctx-option-mismatch",
            "quote_token": token,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "QUOTE_CONTRACT_MISMATCH"


def test_option_market_closed_returns_OPTION_MARKET_CLOSED(client, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from xenon.api import server

    after_hours = datetime(2026, 4, 22, 17, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(server, "_now", lambda: after_hours, raising=False)

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
            "limitPrice": 5.00,
            "con_id": 756733,
            "client_attempt_id": "ctx-closed",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["reason_code"] == "OPTION_MARKET_CLOSED"


def test_futu_scope_rejects_order_as_READ_ONLY_BROKER(client, monkeypatch):
    from xenon.api import server

    monkeypatch.setattr(server.app.state, "broker", "FUTU", raising=False)
    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 500.20,
            "client_attempt_id": "futu-readonly-1",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["reason_code"] == "READ_ONLY_BROKER"


def test_futu_scope_rejects_before_ib_mode_verification(client, monkeypatch):
    from xenon.api import server

    monkeypatch.setattr(server.app.state, "broker", "FUTU", raising=False)
    monkeypatch.setattr(server.app.state, "mode_verified", False, raising=False)
    resp = client.post(
        "/orders/place",
        json={
            "type": "stock",
            "symbol": "SPY",
            "action": "BUY",
            "quantity": 1,
            "limitPrice": 500.20,
            "client_attempt_id": "futu-readonly-2",
        },
    )
    assert resp.status_code == 403
    assert resp.json()["reason_code"] == "READ_ONLY_BROKER"


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
    sync_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql+psycopg://")
    engine = create_engine(sync_url, pool_pre_ping=True)
    try:
        with engine.connect() as con:
            rows = con.execute(text("SELECT kind FROM xenon.order_events")).fetchall()
    finally:
        engine.dispose()
    assert ("PREFLIGHT_ACK_LIMIT",) in rows
