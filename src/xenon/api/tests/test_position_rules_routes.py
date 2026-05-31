from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text

from xenon.api import server as server_mod
from xenon.db.engine import get_sync_engine
from xenon.db.queries.position_protection import get_by_id, insert_pending_arm


def _descriptor(symbol: str = "APICANCEL") -> dict:
    return {
        "asset_class": "stock",
        "anchor_price": 100.0,
        "opened_qty": 100,
        "protected_qty": 100,
        "multiplier": 1,
        "qty_unit": "share",
        "opened_at": "2026-05-04T14:00:00Z",
        "source": "test",
        "anchor_currency": "USD",
        "legs": [{"sec_type": "STK", "symbol": symbol, "action": "BUY", "ratio": 1, "fill_price": 100.0, "con_id": 1}],
    }


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


def test_position_rules_live_cancel_requires_auth_config():
    app = server_mod.app
    old_mode = getattr(app.state, "trading_mode", None)
    old_account = getattr(app.state, "account", None)
    old_verified = getattr(app.state, "mode_verified", None)
    app.state.trading_mode = "live"
    app.state.account = "U1234567"
    app.state.mode_verified = True
    try:
        client = TestClient(app)
        res = client.post("/position-rules/999999999/cancel")
    finally:
        app.state.trading_mode = old_mode
        app.state.account = old_account
        app.state.mode_verified = old_verified

    assert res.status_code == 503
    assert res.json()["reason_code"] == "live_trading_auth_unconfigured"


def test_position_rules_localhost_live_cancel_uses_bypass_identity(monkeypatch):
    app = server_mod.app
    old_mode = getattr(app.state, "trading_mode", None)
    old_account = getattr(app.state, "account", None)
    old_verified = getattr(app.state, "mode_verified", None)
    app.state.trading_mode = "live"
    app.state.account = "U1234567"
    app.state.mode_verified = True
    monkeypatch.setenv("CLERK_JWKS_URL", "https://clerk.example/.well-known/jwks.json")
    monkeypatch.setenv("CLERK_ISSUER", "https://clerk.example")
    try:
        client = TestClient(app, client=("127.0.0.1", 50000))
        res = client.post("/position-rules/999999999/cancel")
    finally:
        app.state.trading_mode = old_mode
        app.state.account = old_account
        app.state.mode_verified = old_verified

    assert res.status_code == 404


def test_position_rules_cancel_is_scoped(monkeypatch):
    app = server_mod.app
    old_mode = getattr(app.state, "trading_mode", None)
    old_account = getattr(app.state, "account", None)
    old_verified = getattr(app.state, "mode_verified", None)
    app.state.trading_mode = "paper"
    app.state.account = "DU1234567"
    app.state.mode_verified = True
    engine = get_sync_engine()
    position_key = "STK::APICROSSLIVE"
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM xenon.position_close_claims WHERE position_key = :position_key"), {"position_key": position_key})
        conn.execute(text("DELETE FROM xenon.position_protection WHERE position_key = :position_key"), {"position_key": position_key})
    protection_id = insert_pending_arm(
        engine,
        broker="IB",
        account_env="live",
        broker_account="U1234567",
        position_key=position_key,
        position_descriptor=_descriptor("APICROSSLIVE"),
        asset_class="stock",
        rule_kind="stop_loss",
        config={"threshold_pct": -0.08, "anchor": "entry_price"},
    )
    assert protection_id is not None

    try:
        client = TestClient(app)
        res = client.post(f"/position-rules/{protection_id}/cancel")
        row = get_by_id(engine, protection_id=protection_id)
    finally:
        app.state.trading_mode = old_mode
        app.state.account = old_account
        app.state.mode_verified = old_verified
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM xenon.position_close_claims WHERE claimed_by_protection_id = :pid"), {"pid": protection_id})
            conn.execute(text("DELETE FROM xenon.position_protection WHERE protection_id = :pid"), {"pid": protection_id})

    assert res.status_code == 404
    assert row is not None
    assert row["state"] == "PENDING_ARM"
