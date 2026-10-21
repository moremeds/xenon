"""GET /ticker/ratings — Unusual Whales analyst ratings passthrough.

Fixture: real UW `screener/analysts` response for MSFT, captured once on
2026-10-21 (see fixtures/uw_analysts_msft_2026-10-21.json `_provenance`).
No network at test time: `UWClient` is replaced with a fake.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from xenon.api.routes import ratings
from xenon.clients.uw_client import UWAPIError, UWAuthError

FIXTURE = Path(__file__).parent / "fixtures" / "uw_analysts_msft_2026-10-21.json"


@pytest.fixture(scope="module")
def uw_payload() -> dict:
    return json.loads(FIXTURE.read_text())


class FakeUW:
    calls: list[tuple[str, dict]] = []
    payload: dict = {}
    error: Exception | None = None

    def __init__(self, **kwargs):
        self.init_kwargs = kwargs

    def get_analyst_ratings_by_ticker(self, ticker, **kwargs):
        FakeUW.calls.append((ticker, kwargs))
        if FakeUW.error is not None:
            raise FakeUW.error
        return FakeUW.payload

    def close(self):
        pass


@pytest.fixture
def client(monkeypatch, uw_payload):
    FakeUW.calls = []
    FakeUW.payload = uw_payload
    FakeUW.error = None
    monkeypatch.setattr(ratings, "UWClient", FakeUW)
    ratings._cache.clear()
    app = FastAPI()
    app.include_router(ratings.router)
    yield TestClient(app)
    ratings._cache.clear()


def test_returns_raw_uw_rows_with_as_of_and_source(client, uw_payload):
    res = client.get("/ticker/ratings", params={"ticker": "msft"})
    assert res.status_code == 200
    body = res.json()
    assert body["ticker"] == "MSFT"
    assert body["source"] == "unusual_whales"
    assert body["as_of"].endswith("Z")
    assert body["ratings"] == uw_payload["data"]
    # raw fields, no derived scoring
    assert set(body) == {"ticker", "source", "as_of", "ratings"}
    assert FakeUW.calls[0][0] == "MSFT"


def test_second_call_is_served_from_cache(client):
    first = client.get("/ticker/ratings", params={"ticker": "MSFT"}).json()
    second = client.get("/ticker/ratings", params={"ticker": "MSFT"}).json()
    assert len(FakeUW.calls) == 1
    assert second == first  # as_of is the original fetch time


def test_cache_expires_after_ttl(client, monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(ratings, "_now", lambda: t[0])
    client.get("/ticker/ratings", params={"ticker": "MSFT"})
    t[0] += ratings.CACHE_TTL_SECONDS - 1
    client.get("/ticker/ratings", params={"ticker": "MSFT"})
    assert len(FakeUW.calls) == 1
    t[0] += 2
    client.get("/ticker/ratings", params={"ticker": "MSFT"})
    assert len(FakeUW.calls) == 2


def test_cache_is_per_ticker(client):
    client.get("/ticker/ratings", params={"ticker": "MSFT"})
    client.get("/ticker/ratings", params={"ticker": "AAPL"})
    assert [c[0] for c in FakeUW.calls] == ["MSFT", "AAPL"]


@pytest.mark.parametrize("bad", ["", "   ", "TOOLONGTICKER1", "A B", "A/B", "AA$", "..%2F"])
def test_invalid_ticker_is_400(client, bad):
    res = client.get("/ticker/ratings", params={"ticker": bad})
    assert res.status_code == 400
    assert res.json()["reason_code"] == "INVALID_TICKER"
    assert FakeUW.calls == []


def test_missing_ticker_is_400(client):
    res = client.get("/ticker/ratings")
    assert res.status_code == 400
    assert res.json()["reason_code"] == "INVALID_TICKER"


def test_class_share_ticker_allowed(client):
    assert client.get("/ticker/ratings", params={"ticker": "brk.b"}).status_code == 200
    assert FakeUW.calls[0][0] == "BRK.B"


@pytest.mark.parametrize(
    "exc,reason",
    [
        (UWAPIError("Connection failed after 2 attempts: timeout"), "UW_UPSTREAM_ERROR"),
        (UWAuthError("UW_TOKEN environment variable is not set"), "UW_UPSTREAM_ERROR"),
        (RuntimeError("boom"), "UW_UPSTREAM_ERROR"),
    ],
)
def test_uw_failure_is_502_with_top_level_reason_code(client, exc, reason):
    FakeUW.error = exc
    res = client.get("/ticker/ratings", params={"ticker": "MSFT"})
    assert res.status_code == 502
    body = res.json()
    assert body["reason_code"] == reason  # top level, not under .detail
    assert "not set" not in json.dumps(body)  # upstream exception text is not echoed


def test_failure_is_not_cached(client):
    FakeUW.error = UWAPIError("down")
    assert client.get("/ticker/ratings", params={"ticker": "MSFT"}).status_code == 502
    FakeUW.error = None
    assert client.get("/ticker/ratings", params={"ticker": "MSFT"}).status_code == 200


def test_unexpected_payload_shape_is_502(client):
    FakeUW.payload = {"unexpected": []}
    res = client.get("/ticker/ratings", params={"ticker": "MSFT"})
    assert res.status_code == 502
    assert res.json()["reason_code"] == "UW_UPSTREAM_ERROR"


def test_empty_data_is_200_empty_list(client):
    FakeUW.payload = {"data": []}
    res = client.get("/ticker/ratings", params={"ticker": "ZZZZ"})
    assert res.status_code == 200
    assert res.json()["ratings"] == []


def test_router_registered_in_server():
    from xenon.api import server

    paths = {getattr(r, "path", None) for r in server.app.routes}
    assert "/ticker/ratings" in paths
