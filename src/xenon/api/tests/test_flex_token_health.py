"""Flex token expiry on GET /health + /admin/operator (spec §2.9; replaces
the monitor daemon's flex_token_check handler)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from xenon.api.services import flex_token
from xenon.api.routes import system as system_routes

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _write(tmp_path, **cfg):
    p = tmp_path / "flex_token_config.json"
    p.write_text(json.dumps(cfg))
    return p


@pytest.mark.parametrize(
    "expires,days,active,warn,expired",
    [
        ("2027-01-30T12:00:00+00:00", 102, None, False, False),
        ("2026-11-19T12:00:00+00:00", 30, 30, True, False),
        ("2026-10-25T12:00:00+00:00", 5, 7, True, False),
        ("2026-10-20T12:00:00", 0, 1, True, True),  # naive → UTC, like the daemon
    ],
)
def test_thresholds_match_daemon(tmp_path, expires, days, active, warn, expired):
    p = _write(tmp_path, expires_at=expires, reminder_days=[30, 14, 7, 1])
    s = flex_token.flex_token_status(p, now=NOW)
    assert s["configured"] is True
    assert s["days_remaining"] == days
    assert s["active_threshold"] == active
    assert s["should_warn"] is warn
    assert s["expired"] is expired


def test_default_reminder_days(tmp_path):
    p = _write(tmp_path, expires_at="2026-11-01T12:00:00+00:00")
    s = flex_token.flex_token_status(p, now=NOW)
    assert s["reminder_days"] == [30, 14, 7, 1]
    assert s["active_threshold"] == 14


def test_missing_and_invalid_config(tmp_path):
    assert flex_token.flex_token_status(tmp_path / "nope.json") == {"configured": False}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert flex_token.flex_token_status(bad)["configured"] is False


def test_status_does_not_write_config(tmp_path):
    p = _write(tmp_path, expires_at="2026-10-21T12:00:00+00:00")
    before = p.read_text()
    flex_token.flex_token_status(p, now=NOW)
    assert p.read_text() == before


@pytest.mark.parametrize("path", ["/health", "/admin/operator"])
def test_health_and_operator_expose_flex_token(monkeypatch, tmp_path, path):
    from xenon.api import server

    async def fake_gateway():
        return {"port_listening": True}

    monkeypatch.setattr(system_routes, "check_ib_gateway", fake_gateway)
    monkeypatch.setattr(
        flex_token, "CONFIG_PATH", _write(tmp_path, expires_at="2099-01-01T00:00:00+00:00")
    )
    body = TestClient(server.app).get(path).json()
    assert body["flex_token"]["configured"] is True
    assert body["flex_token"]["expires_at"] == "2099-01-01T00:00:00+00:00"
    assert body["flex_token"]["should_warn"] is False


@pytest.mark.parametrize(
    "cfg",
    [
        {"expires_at": "2027-01-01T00:00:00+00:00", "reminder_days": ["invalid"]},
        {"expires_at": "2027-01-01T00:00:00+00:00", "reminder_days": [None]},
        {"expires_at": "2027-01-01T00:00:00+00:00", "reminder_days": 7},
        {"expires_at": 20270101},
        {"expires_at": "not-a-date"},
    ],
)
def test_malformed_config_returns_invalid_state(tmp_path, cfg):
    s = flex_token.flex_token_status(_write(tmp_path, **cfg), now=NOW)
    assert s == {"configured": False, "error": "invalid flex_token_config"}


def test_malformed_config_keeps_health_up(monkeypatch, tmp_path):
    from xenon.api import server

    async def fake_gateway():
        return {"port_listening": True}

    monkeypatch.setattr(system_routes, "check_ib_gateway", fake_gateway)
    monkeypatch.setattr(
        flex_token,
        "CONFIG_PATH",
        _write(tmp_path, expires_at="2027-01-01T00:00:00+00:00", reminder_days=["invalid"]),
    )
    r = TestClient(server.app).get("/health")
    assert r.status_code == 200
    assert r.json()["flex_token"]["configured"] is False
