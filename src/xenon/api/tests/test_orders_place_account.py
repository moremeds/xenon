"""/orders/place forwards the reserved scope account to xenon-ib-place-order.

The reservation row is stamped with the app.state scope account; the IB order
must go to that same account, never to an account named by the request body
and never to IB's default. Account failures surface a top-level reason_code.

No DB, no IB: orders_store, preflight and the subprocess runner are stubbed.
"""

from __future__ import annotations

import json

import pytest

from xenon.api import server as server_mod
from xenon.api.subprocess import ScriptResult
from xenon.execution import orders_store
from xenon.execution.preflight import Verdict

pytestmark = pytest.mark.asyncio

_COMBO = {
    "type": "combo",
    "symbol": "SPY",
    "action": "BUY",
    "quantity": 1,
    "limitPrice": 1.0,
    "legs": [
        {"expiry": "20261120", "strike": 500, "right": "C", "action": "BUY"},
        {"expiry": "20261120", "strike": 510, "right": "C", "action": "SELL"},
    ],
    "client_attempt_id": "acct-1",
}


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setenv("XENON_API_TEST_MODE", "0")
    monkeypatch.setattr(server_mod, "ensure_order_allowed", lambda *_a, **_k: None)

    async def _accept(*_a, **_k):
        return Verdict(accept=True)

    monkeypatch.setattr(server_mod, "_run_preflight", _accept)
    monkeypatch.setattr(server_mod.app.state, "trading_mode", "live", raising=False)
    monkeypatch.setattr(server_mod.app.state, "account", "U2222222", raising=False)
    monkeypatch.setattr(server_mod.app.state, "broker", "IB", raising=False)

    calls: dict = {"terminal": [], "events": [], "runner": []}
    monkeypatch.setattr(
        orders_store,
        "reserve_attempt",
        lambda *_a, **_k: orders_store.ReservationOutcome(
            status="winner", submission_id="sub-1", state=None, duplicate_of=None, reason_code=None
        ),
    )
    monkeypatch.setattr(orders_store, "mark_terminal", lambda **kw: calls["terminal"].append(kw))
    monkeypatch.setattr(orders_store, "record_event", lambda *a, **_k: calls["events"].append(a))
    monkeypatch.setattr(orders_store, "mark_submitted", lambda **_kw: None)

    def set_result(data: dict):
        async def _runner(entry, args, timeout=30):
            calls["runner"].append((entry, json.loads(args[1])))
            return ScriptResult(ok=True, data=data)

        monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", _runner)

    calls["set_result"] = set_result
    return calls


async def test_place_forwards_scope_account_over_body_account(harness):
    harness["set_result"]({"status": "ok", "orderId": 1, "permId": 2})

    await server_mod._orders_place_from_body({**_COMBO, "account": "U1111111"})

    entry, payload = harness["runner"][0]
    assert entry == "xenon-ib-place-order"
    assert payload["account"] == "U2222222"


async def test_place_account_not_managed_returns_top_level_reason_code(harness):
    harness["set_result"](
        {"status": "error", "reason_code": "ACCOUNT_NOT_MANAGED", "message": "account 'U2222222' is not managed"}
    )

    resp = await server_mod._orders_place_from_body(dict(_COMBO))

    assert resp.status_code == 502
    body = json.loads(resp.body)
    assert body["reason_code"] == "ACCOUNT_NOT_MANAGED"
    assert "not managed" in body["detail"]
    assert harness["terminal"][0]["reason_code"] == "ACCOUNT_NOT_MANAGED"
    assert harness["terminal"][0]["state"] == "REJECTED"


async def test_place_ib_435_maps_to_account_unresolved(harness):
    harness["set_result"](
        {
            "status": "error",
            "code": 435,
            "reason_code": "ACCOUNT_UNRESOLVED",
            "message": "IB error 435: You must specify an account.",
        }
    )

    resp = await server_mod._orders_place_from_body(dict(_COMBO))

    body = json.loads(resp.body)
    assert body["reason_code"] == "ACCOUNT_UNRESOLVED"
    assert harness["terminal"][0]["reason_code"] == "ACCOUNT_UNRESOLVED"
