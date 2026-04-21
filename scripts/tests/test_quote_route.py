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

    def fake_fetch(ticker: str, con_id: int):
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
