from __future__ import annotations

from fastapi.testclient import TestClient

from xenon.api import server as server_mod


def test_position_rules_list_and_health_routes():
    client = TestClient(server_mod.app)

    list_res = client.get("/position-rules")
    assert list_res.status_code == 200
    assert isinstance(list_res.json(), list)

    health_res = client.get("/position-rules/health")
    assert health_res.status_code == 200
    body = health_res.json()
    assert "market_window" in body
    assert "rule_counts_by_state" in body
    assert "claim_counts_by_status" in body


def test_position_rules_cancel_unknown_returns_404():
    client = TestClient(server_mod.app)
    res = client.post("/position-rules/999999999/cancel")
    assert res.status_code == 404


def test_position_rules_live_sweep_apply_requires_auth_config():
    app = server_mod.app
    old_mode = getattr(app.state, "trading_mode", None)
    old_account = getattr(app.state, "account", None)
    old_verified = getattr(app.state, "mode_verified", None)
    app.state.trading_mode = "live"
    app.state.account = "U1234567"
    app.state.mode_verified = True
    try:
        client = TestClient(app)
        res = client.post("/position-rules/sweep", json={"apply": True})
    finally:
        app.state.trading_mode = old_mode
        app.state.account = old_account
        app.state.mode_verified = old_verified

    assert res.status_code == 503
    assert res.json()["reason_code"] == "live_trading_auth_unconfigured"
