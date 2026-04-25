"""Wizard order-mutating routes return 503 when trading mode is unverified.

`/wizard/sessions/{id}/submit` and `/protect` reach IB via in-process calls
(submit_combo → server._orders_place_from_body; protect → ib_pool.acquire +
ib_adapter.place_combo_tp), which bypasses any guards on the four `/orders/*`
routes. Both wizard routes must apply require_mode_verified directly.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_with_mismatch(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "1")
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    import xenon.api.trading_mode as tm

    importlib.reload(tm)
    import xenon.api.server as server

    importlib.reload(server)
    monkeypatch.setattr(server, "_get_managed_account_for_health", lambda: "DU1111111")
    with TestClient(server.app) as c:
        yield c


@pytest.mark.parametrize(
    "path,body",
    [
        ("/wizard/sessions/abc/submit", {}),
        ("/wizard/sessions/abc/reprice", {"target_price": "1.00"}),
        ("/wizard/sessions/abc/abort", {}),
        (
            "/wizard/sessions/abc/protect",
            {
                "tp_target_price": "1.00",
                "alert_net_mid_threshold": "0.50",
                "polarity": "DEBIT",
            },
        ),
    ],
)
def test_wizard_routes_blocked_on_mismatch(client_with_mismatch, path, body):
    r = client_with_mismatch.post(path, json=body)
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert "trading mode" in detail.lower()
    assert "live" in detail.lower()
    assert "DU1111111" in detail
