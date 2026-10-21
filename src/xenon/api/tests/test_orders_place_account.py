"""/orders/place forwards the reserved scope account to the place core.

The reservation row is stamped with the app.state scope account; the IB order
must go to that same account, never to an account named by the request body
and never to IB's default. Account failures surface a top-level reason_code.
The row stores the order owner's clientId (base+4).

No DB, no IB: orders_store, preflight and the place core (answered through
the patched runner, scripts/tests/order_owner_fakes.py) are stubbed.
"""

from __future__ import annotations

import json

import pytest

from xenon.api import server as server_mod
from xenon.api.subprocess import ScriptResult
from xenon.execution import gate4, orders_store
from xenon.clients.ib_client import pool_client_id
from xenon.execution.preflight import Verdict
from xenon.api.services import order_entry

OWNER_ID = pool_client_id("orders")

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
    monkeypatch.setattr(order_entry, "ensure_order_allowed", lambda *_a, **_k: None)

    async def _accept(*_a, **_k):
        return Verdict(accept=True)

    monkeypatch.setattr(order_entry, "_run_preflight", _accept)
    monkeypatch.setattr(server_mod.app.state, "trading_mode", "live", raising=False)
    monkeypatch.setattr(server_mod.app.state, "account", "U2222222", raising=False)
    monkeypatch.setattr(server_mod.app.state, "broker", "IB", raising=False)

    calls: dict = {"terminal": [], "events": [], "runner": [], "submitted": [], "unknown": []}

    def _rec(key):
        # Fake store writes "win" (rowcount 1); expected_states guards are
        # covered against Postgres in scripts/tests/test_gate4_route.py.
        def _write(**kw):
            calls[key].append({k: v for k, v in kw.items() if k != "expected_states"})
            return 1

        return _write

    # PR-03b: the place route reserves through Gate 4 (coverage + reservation
    # in one locked txn); stub that seam and the IB leg-meta lookup.
    async def _no_meta(_body):
        return None

    monkeypatch.setattr(order_entry, "_resolve_gate4_leg_meta", _no_meta)
    monkeypatch.setattr(
        gate4,
        "reserve_with_coverage",
        lambda *_a, **_k: gate4.Gate4Outcome(status="winner", submission_id="sub-1"),
    )
    monkeypatch.setattr(orders_store, "mark_terminal", _rec("terminal"))
    monkeypatch.setattr(orders_store, "mark_place_unknown", _rec("unknown"))
    monkeypatch.setattr(orders_store, "record_event", lambda *a, **_k: calls["events"].append(a))
    monkeypatch.setattr(orders_store, "mark_submitted", _rec("submitted"))

    def set_result(data: dict):
        async def _runner(entry, args, timeout=30):
            calls["runner"].append((entry, json.loads(args[1])))
            return ScriptResult(ok=True, data=data)

        monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", _runner)

    calls["set_result"] = set_result
    # PR-10: the place core runs on the order owner; its fake answers through
    # the runner above (scripts/tests/order_owner_fakes.py).
    from scripts.tests.order_owner_fakes import install_runner_cores

    install_runner_cores(monkeypatch)
    return calls


async def test_place_forwards_scope_account_over_body_account(harness):
    harness["set_result"]({"status": "ok", "orderId": 1, "permId": 2})

    await order_entry._orders_place_from_body({**_COMBO, "account": "U1111111"})

    entry, payload = harness["runner"][0]
    assert entry == "xenon-ib-place-order"
    assert payload["account"] == "U2222222"


async def test_place_account_not_managed_returns_top_level_reason_code(harness):
    harness["set_result"](
        {"status": "error", "reason_code": "ACCOUNT_NOT_MANAGED", "message": "account 'U2222222' is not managed"}
    )

    resp = await order_entry._orders_place_from_body(dict(_COMBO))

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

    resp = await order_entry._orders_place_from_body(dict(_COMBO))

    body = json.loads(resp.body)
    assert body["reason_code"] == "ACCOUNT_UNRESOLVED"
    assert harness["terminal"][0]["reason_code"] == "ACCOUNT_UNRESOLVED"


# ---------------------------------------------------------------------------
# A live order is never marked REJECTED (2026-10-17 399-warning incident)
# ---------------------------------------------------------------------------


async def test_place_validation_error_without_perm_id_is_rejected(harness):
    # ib_async's ValidationError label is not broker acceptance (321 / 434).
    harness["set_result"](
        {
            "status": "error",
            "code": 434,
            "message": "IB error 434: The order size cannot be zero.",
            "orderId": 7,
            "permId": 0,
            "initialStatus": "ValidationError",
        }
    )

    resp = await order_entry._orders_place_from_body(dict(_COMBO))

    assert resp.status_code == 502
    assert harness["terminal"][0]["state"] == "REJECTED"
    assert harness["submitted"] == []


async def test_place_110_returns_top_level_limit_off_tick(harness):
    harness["set_result"](
        {
            "status": "error",
            "code": 110,
            "message": "IB error 110: The price does not conform to the minimum price variation for this contract.",
            "orderId": 7,
            "permId": 0,
            "initialStatus": "Cancelled",
        }
    )

    resp = await order_entry._orders_place_from_body(dict(_COMBO))

    assert resp.status_code == 502
    assert json.loads(resp.body)["reason_code"] == "LIMIT_OFF_TICK"
    assert harness["terminal"][0]["reason_code"] == "LIMIT_OFF_TICK"


@pytest.mark.parametrize("data", [{}, None])
async def test_place_empty_cli_output_marks_unknown_not_pending(harness, data):
    harness["set_result"](data)

    resp = await order_entry._orders_place_from_body(dict(_COMBO))

    assert resp.status_code == 502
    body = json.loads(resp.body)
    assert body["reason_code"] == "PLACE_OUTCOME_UNKNOWN"
    assert "working orders" in body["detail"].lower()
    assert "do not resubmit" in body["detail"].lower()
    # One conditional PENDING → UNKNOWN write (never demotes poller evidence).
    assert harness["unknown"] == [
        {
            "submission_id": "sub-1",
            "ib_order_id": None,
            "perm_id": None,
            "placing_client_id": OWNER_ID,
            "reason_code": "PLACE_OUTCOME_UNKNOWN",
        }
    ]
    assert harness["terminal"] == []
    assert harness["submitted"] == []


async def test_place_stores_the_owner_client_id(harness):
    """PR-10 A3: the owner (base+4) placed it — never the core's or the
    legacy 26 fallback."""
    harness["set_result"]({"status": "ok", "orderId": 7, "permId": 8, "clientId": 20})

    resp = await order_entry._orders_place_from_body(dict(_COMBO))

    assert harness["submitted"][0]["placing_client_id"] == OWNER_ID
    assert resp["clientId"] == OWNER_ID


async def test_place_ok_with_notices_records_ib_notice_event(harness):
    notices = [{"reqId": 7, "code": 399, "message": "Warning: ..."}]
    harness["set_result"](
        {"status": "ok", "orderId": 7, "permId": 123456, "initialStatus": "PreSubmitted", "ibNotices": notices}
    )

    resp = await order_entry._orders_place_from_body(dict(_COMBO))

    assert resp["status"] == "ok"
    assert harness["submitted"][0]["perm_id"] == "123456"
    assert ("sub-1", "IB_NOTICE", {"notices": notices}) in harness["events"]


async def test_place_unconfirmed_399_without_perm_id_goes_working(harness):
    # The CLI payload for a 399 on a PendingSubmit order with no permId
    # inside the wait (scripts/tests/test_ib_place_order_notices.py).
    notices = [{"reqId": 7, "code": 399, "message": "Warning: your order will not be placed until ..."}]
    harness["set_result"](
        {
            "status": "ok",
            "orderId": 7,
            "permId": 0,
            "initialStatus": "ValidationError",
            "clientId": 20,
            "ibNotices": notices,
        }
    )

    resp = await order_entry._orders_place_from_body(dict(_COMBO))

    assert resp["status"] == "ok"
    assert harness["terminal"] == []
    assert harness["submitted"] == [
        {"submission_id": "sub-1", "ib_order_id": "7", "perm_id": "", "placing_client_id": OWNER_ID}
    ]


# ---------------------------------------------------------------------------
# Round 4: the route trusts the CLI's explicit outcome
# ---------------------------------------------------------------------------


async def test_place_outcome_rejected_wins_over_perm_id_and_live_status(harness):
    # PreSubmitted(permId) → 201 → 399: final status ValidationError, permId
    # set, but the CLI timeline says rejected.
    harness["set_result"](
        {
            "status": "error",
            "outcome": "rejected",
            "code": 201,
            "message": "IB error 201: Order rejected - reason: margin",
            "orderId": 7,
            "permId": 123456,
            "initialStatus": "ValidationError",
            "brokerStatus": "PreSubmitted",
        }
    )

    resp = await order_entry._orders_place_from_body(dict(_COMBO))

    assert resp.status_code == 502
    assert json.loads(resp.body)["reason_code"] == "IB_REJECT"
    assert harness["terminal"][0]["state"] == "REJECTED"
    assert harness["submitted"] == []


@pytest.mark.parametrize("outcome", ["accepted", "unconfirmed"])
async def test_place_outcome_accepted_or_unconfirmed_goes_working(harness, outcome):
    harness["set_result"](
        {"status": "ok", "outcome": outcome, "orderId": 7, "permId": 0, "initialStatus": "ValidationError"}
    )

    resp = await order_entry._orders_place_from_body(dict(_COMBO))

    assert resp["status"] == "ok"
    assert harness["terminal"] == []
    assert harness["submitted"][0]["ib_order_id"] == "7"

