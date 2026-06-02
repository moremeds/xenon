"""XENON_READ_ONLY=1 disables write routes (orders/place, cancel, modify, journal).

Set by `scripts/infra/dev.sh live` — keeps dev-machine sessions from
persisting any live-IB-derived data into core_test. Real prod writes go
through the macmini Docker stack which never sets the flag.

Response shape: JSONResponse with top-level `reason_code: READ_ONLY_MODE`
so the web toast helper renders it directly (HTTPException(detail={...})
nests it under .detail and breaks the toast — see
CLAUDE.md memory feedback_httpexception_dict_detail_breaks_toast).
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def read_only_client(monkeypatch):
    """TestClient with XENON_READ_ONLY=1 and mode matched.

    Mode match is needed so the read-only check fires BEFORE the mode
    guard; otherwise a mode-mismatched test would 503 first and we'd
    never reach the 403.
    """
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    import xenon.api.trading_mode as tm

    importlib.reload(tm)
    import xenon.api.server as server

    importlib.reload(server)
    monkeypatch.setattr(server, "_get_managed_account_for_health", lambda: "DU1111111")
    with TestClient(server.app) as c:
        yield c


@pytest.fixture
def writable_client(monkeypatch):
    """Identical setup but without the read-only flag."""
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_TRADING_MODE", "paper")
    monkeypatch.delenv("XENON_READ_ONLY", raising=False)
    import xenon.api.trading_mode as tm

    importlib.reload(tm)
    import xenon.api.server as server

    importlib.reload(server)
    monkeypatch.setattr(server, "_get_managed_account_for_health", lambda: "DU1111111")
    with TestClient(server.app) as c:
        yield c


@pytest.mark.parametrize(
    "path",
    [
        "/orders/place",
        "/orders/cancel",
        "/orders/modify",
    ],
)
def test_write_routes_return_403_in_read_only(read_only_client, path):
    """All three order-mutation routes return 403 with the standard payload."""
    r = read_only_client.post(path, json={})
    assert r.status_code == 403, r.text
    body = r.json()
    assert body.get("reason_code") == "READ_ONLY_MODE", body
    # Top-level reason_code is the toast contract — detail is a human string,
    # NOT a dict containing reason_code (which would nest it one level deep).
    assert isinstance(body.get("detail"), str)
    assert "dev.sh live" in body["detail"] or "XENON_READ_ONLY" in body["detail"]


def test_journal_post_returns_403_in_read_only(read_only_client):
    """POST /journal also blocks — journal writes are user-authored state."""
    r = read_only_client.post(
        "/journal",
        json={"ticker": "SPY", "note": "test"},
    )
    assert r.status_code == 403, r.text
    body = r.json()
    assert body.get("reason_code") == "READ_ONLY_MODE", body


def test_journal_get_succeeds_in_read_only(read_only_client):
    """Reads stay open — the flag only blocks writes."""
    r = read_only_client.get("/journal?days=1&limit=1")
    # In test_mode the journal table is per-test-txn empty; the route
    # itself should still return 200 with an empty trades list.
    assert r.status_code == 200, r.text
    assert "trades" in r.json()


def test_orders_place_succeeds_when_flag_unset(writable_client):
    """Sanity: without XENON_READ_ONLY, the route advances past the guard.

    /orders/place still needs IB plumbing; in test_mode it short-circuits
    earlier paths. We only assert it does NOT return 403/READ_ONLY_MODE.
    """
    r = writable_client.post("/orders/place", json={})
    if r.status_code == 403:
        body = r.json()
        assert body.get("reason_code") != "READ_ONLY_MODE", (
            "Flag-unset client got 403 READ_ONLY_MODE — guard fired on the wrong side. Check monkeypatch ordering."
        )
