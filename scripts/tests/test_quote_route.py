from decimal import Decimal

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_QUOTE_TOKEN_SECRET", "c" * 64)
    yield


@pytest.fixture
def client():
    from xenon.api.server import app

    return TestClient(app)


def test_quote_route_returns_signed_token(client, monkeypatch):
    from xenon.api import server

    async def fake_fetch(ticker: str, con_id: int):
        return {
            "bid": Decimal("500.10"),
            "ask": Decimal("500.20"),
            "bid_size": 100,
            "ask_size": 120,
        }

    monkeypatch.setattr(server, "_fetch_quote_snapshot", fake_fetch)

    resp = client.get("/orders/quote", params={"ticker": "SPY", "con_id": 756733})
    assert resp.status_code == 200
    body = resp.json()
    assert "token" in body
    assert body["bid"] == "500.10"

    from xenon.execution import quote_tokens

    payload = quote_tokens.verify(body["token"], "c" * 64, max_age_ms=5000)
    assert payload.ticker == "SPY"
    assert payload.con_id == 756733


def test_quote_route_runs_snapshot_worker_with_event_loop(client, monkeypatch):
    import asyncio
    import threading

    from xenon.api import server

    seen = {"loop": False, "thread": None, "role": None}

    class FakeClient:
        def qualify_contract(self, contract):
            asyncio.get_event_loop()
            seen["loop"] = True
            seen["thread"] = threading.current_thread().name
            return contract

        def get_quote(self, contract, snapshot=True):
            asyncio.get_event_loop()
            return type(
                "Ticker",
                (),
                {"bid": 1.2, "ask": 1.3, "bidSize": 4, "askSize": 5},
            )()

    class FakeAcquire:
        async def __aenter__(self):
            return FakeClient()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakePool:
        def acquire(self, role):
            seen["role"] = role
            return FakeAcquire()

    monkeypatch.setattr(server, "ib_pool", FakePool())

    resp = client.get("/orders/quote", params={"ticker": "SPY", "con_id": 756733})

    assert resp.status_code == 200, resp.text
    assert seen["role"] == "data"
    assert seen["loop"] is True
    assert seen["thread"] != threading.current_thread().name
