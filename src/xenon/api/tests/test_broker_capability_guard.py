"""Broker capability guard (X3a).

`broker_capability_denial` is the capability check X3c wires into the order
cores; `ensure_order_allowed` uses it for non-IB brokers. In X3a no Futu
dispatch exists, so `ensure_order_allowed` refuses every non-IB write even
when the capability would allow it. Denials are JSONResponse with a
top-level `reason_code` (never HTTPException(detail=dict): the toast reads
`body.reason_code`). XENON_READ_ONLY=1 always wins first.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from xenon.api import guards
from xenon.execution import broker_capabilities

ACC = "281756479345015383"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("XENON_READ_ONLY", "XENON_FUTU_TRADING", "FUTU_TRADE_ACC_ID_SIMULATE", "FUTU_TRADE_ACC_ID_REAL"):
        monkeypatch.delenv(name, raising=False)
    # No stored scope with unresolved rows (the veto has its own tests in
    # scripts/tests/test_futu_scope_switch.py).
    monkeypatch.setattr(guards, "futu_scope_change_pending", lambda env: [])


def _enable_futu(monkeypatch):
    monkeypatch.setenv("XENON_FUTU_TRADING", "simulate")
    monkeypatch.setenv("FUTU_TRADE_ACC_ID_SIMULATE", ACC)


def _body(resp):
    assert isinstance(resp, JSONResponse)
    return json.loads(resp.body)


def _state(broker="IB", mode_verified=True):
    return SimpleNamespace(broker=broker, mode_verified=mode_verified, trading_mode="paper", account="DU1")


# ── broker_capability_denial ────────────────────────────────────


@pytest.mark.parametrize("op", broker_capabilities.OPERATIONS)
def test_futu_default_denied_with_top_level_reason_code(op):
    resp = guards.broker_capability_denial("FUTU", op, "order placement")
    assert resp.status_code == 403
    body = _body(resp)
    assert body["reason_code"] == "READ_ONLY_BROKER"
    assert "FUTU is read-only for order placement" in body["detail"]
    assert body["reason_detail"] == body["detail"]


@pytest.mark.parametrize("op", ["place_single", "cancel", "modify"])
def test_futu_enabled_gated_ops_pass(monkeypatch, op):
    _enable_futu(monkeypatch)
    assert guards.broker_capability_denial("FUTU", op, "x", "paper") is None
    assert guards.broker_capability_denial("FUTU", op, "x", "live") is not None  # REAL not enabled


@pytest.mark.parametrize("op", ["replace", "combo", "wizard", None])
def test_futu_enabled_still_denies_replace_combo_wizard(monkeypatch, op):
    _enable_futu(monkeypatch)
    resp = guards.broker_capability_denial("FUTU", op, "order replacement")
    assert _body(resp)["reason_code"] == "READ_ONLY_BROKER"


@pytest.mark.parametrize("op", broker_capabilities.OPERATIONS)
def test_read_only_precedes_capability(monkeypatch, op):
    _enable_futu(monkeypatch)
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    for broker in ("IB", "FUTU"):
        resp = guards.broker_capability_denial(broker, op, "x")
        assert resp.status_code == 403
        assert _body(resp)["reason_code"] == "READ_ONLY_MODE"


@pytest.mark.parametrize("op", broker_capabilities.OPERATIONS)
def test_ib_allowed(op):
    assert guards.broker_capability_denial("IB", op, "x") is None


# ── ensure_order_allowed ────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "op"),
    [
        ("order placement", "place_single"),
        ("order cancellation", "cancel"),
        ("order modification", "modify"),
        ("order replacement", "replace"),
        ("naked-short audit cancel", "cancel"),
        ("wizard submit", "wizard"),
        ("wizard protect", "wizard"),
        ("something new", None),
    ],
)
def test_kind_maps_to_operation(kind, op):
    assert guards._op_for_kind(kind) == op


def test_ensure_ib_unchanged():
    assert guards.ensure_order_allowed("order placement", _state("IB")) is None
    assert guards.ensure_order_allowed("something new", _state("IB")) is None
    with pytest.raises(HTTPException) as exc:
        guards.ensure_order_allowed("order placement", _state("IB", mode_verified=False))
    assert exc.value.status_code == 503


_DISPATCHED = ["order placement", "order cancellation", "order modification"]


@pytest.mark.parametrize("kind", [*_DISPATCHED, "order replacement", "wizard submit"])
@pytest.mark.parametrize("enabled", [False, True])
def test_ensure_futu_refused_unless_enabled_and_dispatched(monkeypatch, kind, enabled):
    """X3c: an enabled env passes place / cancel / modify to the Futu
    dispatch (None); replace and wizard stay refused; nothing passes while
    the env is not enabled."""
    if enabled:
        _enable_futu(monkeypatch)
    resp = guards.ensure_order_allowed(kind, _state("FUTU", mode_verified=False), account_env="paper")
    if enabled and kind in _DISPATCHED:
        assert resp is None
        return
    assert resp.status_code == 403
    body = _body(resp)
    assert body["reason_code"] == "READ_ONLY_BROKER"
    assert f"FUTU is read-only for {kind}" in body["detail"]


def test_ensure_futu_explicit_op_overrides_kind(monkeypatch):
    _enable_futu(monkeypatch)
    resp = guards.ensure_order_allowed("order placement", _state("FUTU"), op="combo")
    assert "combo" in _body(resp)["detail"]  # never-supported op: 403 even without account_env


@pytest.mark.parametrize("kind", ["order placement", "order cancellation", "order modification"])
@pytest.mark.parametrize("env", [None, "", "PAPER", "simulate", "x"])
def test_ensure_futu_mutation_needs_account_env(monkeypatch, kind, env):
    _enable_futu(monkeypatch)
    resp = guards.ensure_order_allowed(kind, _state("FUTU"), account_env=env)
    assert resp.status_code == 400
    assert _body(resp)["reason_code"] == "FUTU_SCOPE_REQUIRED"


def test_ensure_unknown_broker_refused():
    resp = guards.ensure_order_allowed("order placement", _state("SCHWAB"))
    assert _body(resp)["reason_code"] == "READ_ONLY_BROKER"


def test_ensure_read_only_wins_over_futu(monkeypatch):
    _enable_futu(monkeypatch)
    monkeypatch.setenv("XENON_READ_ONLY", "1")
    resp = guards.ensure_order_allowed("order placement", _state("FUTU"))
    assert _body(resp)["reason_code"] == "READ_ONLY_MODE"
    resp = guards.ensure_order_allowed("order placement", _state("IB", mode_verified=False))
    assert _body(resp)["reason_code"] == "READ_ONLY_MODE"
