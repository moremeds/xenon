import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from xenon.execution import orders_store, quote_tokens

SECRET = "b" * 64


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_QUOTE_TOKEN_SECRET", SECRET)
    db = tmp_path / "orders.duckdb"
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(db))
    orders_store.init_store(db)
    from xenon.api.server import app

    return TestClient(app)


def _mint(con_id: int, bid: str = "4.50", ask: str = "4.70") -> str:
    p = quote_tokens.QuotePayload(
        con_id=con_id,
        ticker="SPY",
        bid=Decimal(bid),
        ask=Decimal(ask),
        bid_size=100,
        ask_size=100,
        ts_server_ms=int(time.time() * 1000),
    )
    return quote_tokens.mint(p, SECRET)


def _combo_body(legs_tokens: dict[str, str] | None, limit_price: str = "2.70"):
    return {
        "type": "combo",
        "symbol": "SPY",
        "action": "BUY",
        "quantity": 1,
        "limitPrice": float(limit_price),
        "tif": "DAY",
        "client_attempt_id": f"attempt-{time.time_ns()}",
        "legs": [
            {"con_id": 1, "expiry": "2026-05-16", "strike": 500, "right": "C", "action": "BUY", "ratio": 1},
            {"con_id": 2, "expiry": "2026-05-16", "strike": 510, "right": "C", "action": "SELL", "ratio": 1},
        ],
        **({"quote_tokens": legs_tokens} if legs_tokens is not None else {}),
    }


def test_combo_missing_tokens_soft_fails_with_telemetry(client):
    body = _combo_body(None)
    r = client.post("/orders/place", json=body)
    assert r.status_code == 200, r.text
    from xenon.execution import orders_store

    con = orders_store._connect_utc(orders_store._resolve_path(None))
    try:
        rows = con.execute("SELECT kind FROM orders_events WHERE kind='QUOTE_TOKEN_MISSING_SOFT'").fetchall()
    finally:
        con.close()
    assert len(rows) == 1


def test_combo_in_band_tokens_pass(client):
    tokens = {"1": _mint(1, "4.50", "4.70"), "2": _mint(2, "2.00", "2.20")}
    body = _combo_body(tokens, limit_price="2.70")
    r = client.post("/orders/place", json=body)
    assert r.status_code == 200, r.text


def test_combo_out_of_band_rejects(client):
    tokens = {"1": _mint(1, "4.50", "4.70"), "2": _mint(2, "2.00", "2.20")}
    body = _combo_body(tokens, limit_price="3.50")
    r = client.post("/orders/place", json=body)
    assert r.status_code == 400
    assert r.json()["reason_code"] == "LIMIT_OUT_OF_BAND"


def test_combo_tampered_token_rejects(client):
    tokens = {"1": _mint(1, "4.50", "4.70") + "x", "2": _mint(2, "2.00", "2.20")}
    body = _combo_body(tokens)
    r = client.post("/orders/place", json=body)
    assert r.status_code == 400
    assert r.json()["reason_code"] == "STALE_QUOTE"


def test_combo_out_of_band_emits_quote_check_fail_telemetry(client):
    tokens = {"1": _mint(1, "4.50", "4.70"), "2": _mint(2, "2.00", "2.20")}
    body = _combo_body(tokens, limit_price="50.00")
    r = client.post("/orders/place", json=body)
    assert r.status_code == 400
    # Telemetry row exists with the reason code.
    con = orders_store._connect_utc(orders_store._resolve_path(None))
    try:
        rows = con.execute(
            "SELECT kind, detail FROM orders_events WHERE kind='QUOTE_CHECK_FAIL'"
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 1
    import json as _json
    detail = _json.loads(rows[0][1])
    assert detail["reason_code"] == "LIMIT_OUT_OF_BAND"
    assert detail["leg_count"] == 2


def test_combo_tampered_token_emits_quote_check_fail_telemetry(client):
    tokens = {"1": _mint(1, "4.50", "4.70") + "x", "2": _mint(2, "2.00", "2.20")}
    body = _combo_body(tokens)
    r = client.post("/orders/place", json=body)
    assert r.status_code == 400
    con = orders_store._connect_utc(orders_store._resolve_path(None))
    try:
        rows = con.execute(
            "SELECT kind FROM orders_events WHERE kind='QUOTE_CHECK_FAIL'"
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 1
