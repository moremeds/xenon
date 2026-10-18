"""/orders/place forwards the reserved scope account to xenon-ib-place-order.

The reservation row is stamped with the app.state scope account; the IB order
must go to that same account, never to an account named by the request body
and never to IB's default. Account failures surface a top-level reason_code.
A CLI "error" for an order that is live at IB must never mark it REJECTED.

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

    calls: dict = {"terminal": [], "events": [], "runner": [], "submitted": []}
    monkeypatch.setattr(
        orders_store,
        "reserve_attempt",
        lambda *_a, **_k: orders_store.ReservationOutcome(
            status="winner", submission_id="sub-1", state=None, duplicate_of=None, reason_code=None
        ),
    )
    monkeypatch.setattr(orders_store, "mark_terminal", lambda **kw: calls["terminal"].append(kw))
    monkeypatch.setattr(orders_store, "record_event", lambda *a, **_k: calls["events"].append(a))
    monkeypatch.setattr(orders_store, "mark_submitted", lambda **kw: calls["submitted"].append(kw))

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


# ---------------------------------------------------------------------------
# A live order is never marked REJECTED (2026-10-17 399-warning incident)
# ---------------------------------------------------------------------------


async def test_place_error_payload_with_live_perm_id_is_not_rejected(harness):
    # Older / defensive CLI payload: "error" while IB holds the order live.
    harness["set_result"](
        {
            "status": "error",
            "code": 399,
            "message": "IB error 399: Order Message: Warning: your order will not be placed until ...",
            "orderId": 7,
            "permId": 123456,
            "initialStatus": "PreSubmitted",
        }
    )

    resp = await server_mod._orders_place_from_body(dict(_COMBO))

    assert harness["terminal"] == []
    assert harness["submitted"] == [
        {"submission_id": "sub-1", "ib_order_id": "7", "perm_id": "123456", "placing_client_id": 26}
    ]
    assert resp["status"] == "ok"
    assert resp["permId"] == 123456
    assert any(e[1] == "IB_NOTICE" for e in harness["events"])


async def test_place_error_payload_with_perm_id_but_cancelled_is_rejected(harness):
    harness["set_result"](
        {
            "status": "error",
            "code": 201,
            "message": "IB error 201: Order rejected",
            "orderId": 7,
            "permId": 123456,
            "initialStatus": "Cancelled",
        }
    )

    resp = await server_mod._orders_place_from_body(dict(_COMBO))

    assert resp.status_code == 502
    body = json.loads(resp.body)
    assert body["reason_code"] == "IB_REJECT"
    assert body["detail"] == "IB error 201: Order rejected"
    assert harness["terminal"][0]["state"] == "REJECTED"
    assert harness["terminal"][0]["reason_code"] == "IB_REJECT"
    assert harness["submitted"] == []


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

    resp = await server_mod._orders_place_from_body(dict(_COMBO))

    assert resp.status_code == 502
    assert harness["terminal"][0]["state"] == "REJECTED"
    assert harness["submitted"] == []


async def test_place_error_payload_filled_with_perm_id_is_accepted(harness):
    harness["set_result"](
        {
            "status": "error",
            "code": 399,
            "message": "IB error 399: Warning",
            "orderId": 7,
            "permId": 9,
            "initialStatus": "Filled",
        }
    )

    resp = await server_mod._orders_place_from_body(dict(_COMBO))

    assert harness["terminal"] == []
    assert harness["submitted"][0]["perm_id"] == "9"
    assert resp["status"] == "ok"


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

    resp = await server_mod._orders_place_from_body(dict(_COMBO))

    assert resp.status_code == 502
    assert json.loads(resp.body)["reason_code"] == "LIMIT_OFF_TICK"
    assert harness["terminal"][0]["reason_code"] == "LIMIT_OFF_TICK"


@pytest.mark.parametrize("data", [{}, None])
async def test_place_empty_cli_output_marks_unknown_not_pending(harness, data):
    harness["set_result"](data)

    resp = await server_mod._orders_place_from_body(dict(_COMBO))

    assert resp.status_code == 502
    body = json.loads(resp.body)
    assert body["reason_code"] == "PLACE_OUTCOME_UNKNOWN"
    assert "working orders" in body["detail"].lower()
    assert "do not resubmit" in body["detail"].lower()
    assert harness["terminal"] == [
        {
            "submission_id": "sub-1",
            "state": "UNKNOWN",
            "reason_code": "PLACE_OUTCOME_UNKNOWN",
            "filled_qty": 0,
            "avg_fill_price": None,
        }
    ]
    assert harness["submitted"] == []


async def test_place_stores_the_cli_placing_client_id(harness):
    harness["set_result"]({"status": "ok", "orderId": 7, "permId": 8, "clientId": 20})

    await server_mod._orders_place_from_body(dict(_COMBO))

    assert harness["submitted"][0]["placing_client_id"] == 20


async def test_place_ok_with_notices_records_ib_notice_event(harness):
    notices = [{"reqId": 7, "code": 399, "message": "Warning: ..."}]
    harness["set_result"](
        {"status": "ok", "orderId": 7, "permId": 123456, "initialStatus": "PreSubmitted", "ibNotices": notices}
    )

    resp = await server_mod._orders_place_from_body(dict(_COMBO))

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

    resp = await server_mod._orders_place_from_body(dict(_COMBO))

    assert resp["status"] == "ok"
    assert harness["terminal"] == []
    assert harness["submitted"] == [
        {"submission_id": "sub-1", "ib_order_id": "7", "perm_id": "", "placing_client_id": 20}
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

    resp = await server_mod._orders_place_from_body(dict(_COMBO))

    assert resp.status_code == 502
    assert json.loads(resp.body)["reason_code"] == "IB_REJECT"
    assert harness["terminal"][0]["state"] == "REJECTED"
    assert harness["submitted"] == []


@pytest.mark.parametrize("outcome", ["accepted", "unconfirmed"])
async def test_place_outcome_accepted_or_unconfirmed_goes_working(harness, outcome):
    harness["set_result"](
        {"status": "ok", "outcome": outcome, "orderId": 7, "permId": 0, "initialStatus": "ValidationError"}
    )

    resp = await server_mod._orders_place_from_body(dict(_COMBO))

    assert resp["status"] == "ok"
    assert harness["terminal"] == []
    assert harness["submitted"][0]["ib_order_id"] == "7"


async def test_place_legacy_hard_error_with_perm_id_and_live_status_is_unknown(harness):
    # No ``outcome``: never REJECT (permId > 0, non-terminal) and never
    # accept (hard-error code present) → UNKNOWN.
    harness["set_result"](
        {
            "status": "error",
            "code": 201,
            "message": "IB error 201: Order rejected",
            "orderId": 7,
            "permId": 123456,
            "initialStatus": "PreSubmitted",
            "clientId": 20,
        }
    )

    resp = await server_mod._orders_place_from_body(dict(_COMBO))

    assert resp.status_code == 502
    assert json.loads(resp.body)["reason_code"] == "PLACE_OUTCOME_UNKNOWN"
    assert harness["submitted"] == [
        {"submission_id": "sub-1", "ib_order_id": "7", "perm_id": "123456", "placing_client_id": 20}
    ]
    assert harness["terminal"][0]["state"] == "UNKNOWN"
    assert harness["terminal"][0]["reason_code"] == "PLACE_OUTCOME_UNKNOWN"
