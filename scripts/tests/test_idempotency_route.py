import threading
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

SECRET = "e" * 64


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_QUOTE_TOKEN_SECRET", SECRET)
    monkeypatch.setenv("XENON_ORDERS_DB_PATH", str(tmp_path / "orders.duckdb"))
    monkeypatch.setenv("XENON_DATA_DIR", str(tmp_path))
    import xenon.api.server as server_module

    monkeypatch.setattr(server_module, "test_mode", True, raising=False)
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


def _mint(con_id=756733, ticker="SPY"):
    from xenon.execution import quote_tokens

    p = quote_tokens.QuotePayload(
        con_id=con_id,
        ticker=ticker,
        bid=Decimal("500.10"),
        ask=Decimal("500.20"),
        bid_size=100,
        ask_size=120,
        ts_server_ms=int(time.time() * 1000),
    )
    return quote_tokens.mint(p, SECRET)


def _body(cid, symbol="SPY"):
    return {
        "type": "stock",
        "symbol": symbol,
        "action": "BUY",
        "quantity": 1,
        "limitPrice": 500.20,
        "client_attempt_id": cid,
        "quote_token": _mint(ticker=symbol),
        "con_id": 756733,
    }


def test_double_click_only_one_winner(client):
    cid = "dbl-1"
    r1 = client.post("/orders/place", json=_body(cid))
    r2 = client.post("/orders/place", json=_body(cid))
    assert r1.status_code == 200
    assert r2.status_code == 200
    j1, j2 = r1.json(), r2.json()
    assert ("orderId" in j1) ^ ("duplicate_of" in j1)
    if "duplicate_of" in j1:
        j1, j2 = j2, j1
    assert "orderId" in j1
    assert "duplicate_of" in j2
    assert j2["state"] in ("PENDING", "WORKING")


def test_terminal_replay_returns_409(client):
    from xenon.execution import orders_store

    cid = "term-1"
    r1 = client.post("/orders/place", json=_body(cid))
    assert r1.status_code == 200
    row = orders_store.lookup_by_attempt("local", cid)
    orders_store.mark_terminal(
        submission_id=row.submission_id,
        state="REJECTED",
        reason_code="IB_REJECT_201",
        filled_qty=0,
        avg_fill_price=None,
    )
    r2 = client.post("/orders/place", json=_body(cid))
    assert r2.status_code == 409
    assert r2.json()["reason_code"] == "ATTEMPT_ID_TERMINAL"


def test_concurrent_same_cid_only_one_subprocess(client, monkeypatch):
    from xenon.api import server

    calls = {"n": 0}
    orig = server._next_test_order_ids

    def spy():
        calls["n"] += 1
        return orig()

    monkeypatch.setattr(server, "_next_test_order_ids", spy)

    cid = "conc-1"
    barrier = threading.Barrier(6)
    results: list = []

    def go():
        barrier.wait()
        results.append(client.post("/orders/place", json=_body(cid)))

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r.status_code == 200 for r in results)
    assert calls["n"] == 1
