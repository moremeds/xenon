# ruff: noqa: F811 -- route fixtures are imported by name and re-bound as test args
"""PR-10 T3 — /orders/cancel and /orders/modify on the single order owner.

Positive routing (A1), owner path spawns nothing and connects nothing,
worker-owned Gate 4 settlement (v3 item 4 / A3 amend rules). Real route +
Gate 4 + Postgres; only the broker boundary is faked."""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from scripts.tests.order_owner_fakes import install_cores
from scripts.tests.test_gate4_route import (  # noqa: F401 — fixtures
    OWNER_ID,
    _order_owner,
    _pg,
    _pg_engine,
    _place_bag,
    _submission_rows,
    _test_mode_off,
    broker_calls,
    client,
    fresh_quote,
    ib_data,
)
from xenon.api import server as server_mod
from xenon.api.services import order_entry

pytestmark = pytest.mark.committed_db


@pytest.fixture
def no_spawn_no_connect(monkeypatch):
    """Any subprocess spawn or second IB connection fails the test."""

    def _forbidden(*_a, **_k):
        raise AssertionError("order action must not spawn or connect")

    async def _forbidden_async(*_a, **_k):
        raise AssertionError("order action must not spawn")

    import xenon.clients.ib_client as ib_client_mod

    monkeypatch.setattr(server_mod, "run_entry_point", _forbidden_async)
    monkeypatch.setattr(ib_client_mod.IBClient, "connect", _forbidden)


def _placed(client, attempt="own-1"):
    _place_bag(client, attempt, shares=250)  # placed by the orders connection (OWNER_ID)
    orders_client = server_mod.order_owner._pool.client  # forget the place itself
    orders_client.sent.clear()
    orders_client.ready_checks = 0


def _set_placing_client(cid):
    engine = _pg_engine()
    with engine.begin() as conn:
        conn.execute(text("UPDATE xenon.order_submissions SET placing_client_id = :c"), {"c": cid})
    engine.dispose()


def _row():
    return _submission_rows()[0]._mapping


def _modify(client, seq, price=4.5):
    return client.post(
        "/orders/modify", json={"orderId": 4242, "permId": 777001, "newPrice": price, "modifySequence": seq}
    )


# 0 = TWS / master, 1 = a hand-run tool, 25/26 = legacy subprocess auto range,
# 10 = relay, 3/5 = the other pool roles, 104 = another environment's orders id.
@pytest.mark.parametrize("cid", [0, 1, 25, 26, 10, 3, 5, 104])
@pytest.mark.parametrize("op", ["cancel", "modify"])
def test_routing_matrix_not_owned_is_409_with_no_broker_work(
    op, cid, client, broker_calls, fresh_quote, no_spawn_no_connect, _order_owner
):
    _placed(client)
    _set_placing_client(cid)
    broker_calls.clear()
    if op == "cancel":
        resp = client.post("/orders/cancel", json={"orderId": 4242, "permId": 777001})
    else:
        resp = _modify(client, 1)
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["reason_code"] == "ORDER_NOT_OWNED"  # top level: the toast reads it
    assert "TWS / IBKR Mobile" in body["detail"]["message"]
    assert body["detail"]["originalClientId"] == cid
    assert broker_calls == []  # no core call, no spawn
    assert _order_owner.client.sent == []
    assert _row()["legs"].get("pending_amend_id") is None  # no amend opened
    assert _row()["state"] == "WORKING"


@pytest.mark.parametrize("op", ["cancel", "modify"])
def test_owner_id_takes_the_owner_path_without_spawn_or_connect(
    op, client, broker_calls, fresh_quote, no_spawn_no_connect, _order_owner
):
    _placed(client)
    broker_calls.clear()
    if op == "cancel":
        resp = client.post("/orders/cancel", json={"orderId": 4242, "permId": 777001})
    else:
        resp = _modify(client, 1)
    assert resp.status_code == 200, resp.text
    assert len(broker_calls) == 1  # the faked core, on the orders worker
    assert [s[0] for s in _order_owner.client.sent] == ["cancel" if op == "cancel" else "place"]
    assert _order_owner.client.ready_checks == 1  # one fresh round trip before the core


def _trade(order_id, perm_id, client_id):
    return SimpleNamespace(
        order=SimpleNamespace(orderId=order_id, permId=perm_id, clientId=client_id, account="DU0000000"),
        orderStatus=SimpleNamespace(status="Submitted"),
        contract=SimpleNamespace(secType="STK"),
    )


@pytest.mark.parametrize(
    "snapshot",
    [
        [_trade(4711, 0, 0)],  # TWS order, no submission row
        [_trade(4711, 0, 26), _trade(4711, 0, OWNER_ID)],  # ambiguous snapshot
    ],
    ids=["tws-owner", "ambiguous"],
)
def test_no_row_order_is_routed_by_the_fresh_snapshot(snapshot, client, no_spawn_no_connect, _order_owner, monkeypatch):
    """No submission row → the owner's fresh scoped open-order snapshot
    (REAL cancel core) decides: a foreign or ambiguous owner is refused
    before any send."""
    from xenon.execution import ib_order_manage

    _order_owner.client.get_open_orders = lambda: list(snapshot)
    install_cores(monkeypatch, cancel=ib_order_manage.cancel_order_on_client)
    resp = client.post("/orders/cancel", json={"orderId": 4711})
    assert resp.status_code == 409, resp.text
    assert resp.json()["reason_code"] == "ORDER_NOT_OWNED"
    assert _order_owner.client.sent == []


def test_timeout_while_started_worker_settles_and_late_price_confirmation_frees_next_modify(
    client, broker_calls, fresh_quote, _order_owner, monkeypatch
):
    """Request deadline passes after the job started: HTTP answers
    unconfirmed and leaves the price-only amend generation alone (it never
    settles 'uncertain' for a job the worker owns). The worker's late
    confirmation settles it with the captured amend id; the next modify is
    then accepted."""
    _placed(client)
    monkeypatch.setattr(order_entry, "_ORDER_ACTION_TIMEOUT_S", 0.2)
    go = threading.Event()

    def slow_modify(c, order_id, perm_id, new_price, **kw):
        c.place_order("contract", "order")
        go.wait(5)
        return {"status": "ok", "confirmedPrice": new_price}

    install_cores(monkeypatch, modify=slow_modify)
    first = _modify(client, 1, price=4.5)
    assert first.status_code == 503, first.text
    assert first.json()["detail"]["amend_outcome"] == "unconfirmed"
    amend_id = _row()["legs"]["pending_amend_id"]
    assert amend_id is not None  # still owned by the live worker
    assert _modify(client, 2, price=4.4).json()["reason_code"] == "MODIFY_PENDING"

    go.set()
    import asyncio

    asyncio.run(_order_owner.run_sync("orders", lambda: None))  # the worker finished
    row = _row()
    assert row["legs"]["pending_amend_id"] is None
    assert float(row["limit_price"]) == 4.5

    install_cores(monkeypatch, modify=lambda c, o, p, n, **kw: (c.place_order(1, 2), {"status": "ok"})[1])
    assert _modify(client, 3, price=4.3).status_code == 200


def test_queued_modify_abandoned_at_deadline_releases_the_amend(
    client, broker_calls, fresh_quote, _order_owner, monkeypatch
):
    """Deadline while queued → zero sends, amend settled rejected by the
    request path (the worker will never run it)."""
    import asyncio

    _placed(client)
    monkeypatch.setattr(order_entry, "_ORDER_ACTION_TIMEOUT_S", 0.1)
    release = threading.Event()
    busy = threading.Thread(target=lambda: asyncio.run(_order_owner.run_sync("orders", lambda: release.wait(5))))
    busy.start()
    import time

    time.sleep(0.05)
    resp = _modify(client, 1)
    release.set()
    busy.join(5)
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["amend_outcome"] == "not_sent"
    assert _order_owner.client.sent == []
    assert _row()["legs"]["pending_amend_id"] is None
    assert _modify(client, 2).json().get("reason_code") != "MODIFY_PENDING"


def test_halted_owner_refuses_cancel_with_orders_halted(client, broker_calls, fresh_quote, _order_owner, monkeypatch):
    async def _gw():
        return {"port_listening": True}

    monkeypatch.setattr(server_mod, "check_ib_gateway", _gw)
    _placed(client)
    broker_calls.clear()
    server_mod.order_owner._latch_halt("test")
    resp = client.post("/orders/cancel", json={"orderId": 4242, "permId": 777001})
    assert (resp.status_code, resp.json()["reason_code"]) == (503, "ORDERS_HALTED")
    assert broker_calls == [] and _order_owner.client.sent == []
    monkeypatch.setattr(server_mod, "ib_pool", None)  # the data-role fake has no status()
    health = client.get("/health").json()
    assert health["status"] == "orders_halted" and health["orders_owner"]["halted"] is True


def test_broker_lost_refuses_modify_with_ib_broker_disconnected(client, broker_calls, fresh_quote, _order_owner):
    _placed(client)
    broker_calls.clear()
    _order_owner.client.readiness = "lost"  # 1100
    resp = _modify(client, 1)
    assert (resp.status_code, resp.json()["reason_code"]) == (503, "IB_BROKER_DISCONNECTED")
    assert _row()["legs"]["pending_amend_id"] is None
    _order_owner.client.readiness = "ready"  # 1102
    assert _modify(client, 2).status_code == 200


# -- F4 (Astra T1–T3): a sent cancel without an ack is uncertain ------------


def _real_cancel_core_without_ack(monkeypatch, pool, order_id, perm_id):
    """The real cancel core on a fake orders client whose open orders keep
    showing the order as Submitted: cancelOrder sent, never acknowledged."""
    from scripts.tests.test_order_cores import _client, _trade
    from xenon.execution import ib_order_manage

    trade = _trade(order_id=order_id, perm_id=perm_id, client_id=OWNER_ID)
    trade.order.account = "DU0000000"
    orders_client = _client([[trade]] * 12, client_id=OWNER_ID)
    orders_client.readiness = orders_client.broker_readiness = "ready"
    orders_client.readiness_generation = 0
    orders_client.confirm_broker_ready.return_value = "ready"
    monkeypatch.setattr(order_entry, "ib_order_manage", ib_order_manage)
    pool.client = orders_client
    return orders_client


def test_cancel_sent_without_ack_is_uncertain_not_a_reject(
    client, broker_calls, fresh_quote, _order_owner, monkeypatch
):
    _placed(client)
    orders_client = _real_cancel_core_without_ack(monkeypatch, _order_owner, 4242, 777001)
    resp = client.post("/orders/cancel", json={"orderId": 4242, "permId": 777001})
    body = resp.json()
    assert (resp.status_code, body["reason_code"]) == (503, "IB_CONNECTION")
    assert (body["detail"]["classification"], body["detail"]["amend_outcome"]) == ("unconfirmed", "unconfirmed")
    orders_client.cancel_order.assert_called_once()
    assert _row()["state"] != "CANCELLED"


def test_replace_cancel_sent_without_ack_marks_unknown_and_places_nothing(client, fresh_quote, monkeypatch):
    from scripts.tests.test_gate4_route import _Broker, _original, _replace_body
    from scripts.tests.test_gate4_route import _row as _attempt_row

    broker = _Broker()
    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", broker)
    from scripts.tests.order_owner_fakes import install_runner_cores

    _owner, pool = install_runner_cores(monkeypatch)
    target = _original(client, broker)
    orders_client = _real_cancel_core_without_ack(monkeypatch, pool, target["orderId"], target["permId"])

    resp = client.post("/orders/replace", json=_replace_body(target, "rep-noack"))

    assert (resp.status_code, resp.json()["reason_code"]) == (502, "PLACE_OUTCOME_UNKNOWN")
    assert resp.json()["replacement_placed"] is False
    orders_client.cancel_order.assert_called_once()
    assert broker.places == 0
    assert _attempt_row("rep-noack")["state"] == "UNKNOWN"


# -- T4: placement on the owner (A3) -----------------------------------------


def _real_place_core(monkeypatch, steps):
    """The REAL place core on the owner, over a fake connected client backed
    by a real ib_async wrapper (scripts/tests/test_ib_place_order_notices.py).
    ``steps`` replay during the ack wait. The stand-in reports clientId 26."""
    from scripts.tests.test_ib_place_order_notices import _WrapperClient
    from xenon.execution import ib_place_order

    class _OrdersWrapperClient(_WrapperClient):
        readiness = broker_readiness = "ready"
        readiness_generation = 0

        def confirm_broker_ready(self, timeout=5.0):
            return "ready"

    _WrapperClient.steps = list(steps)
    orders_client = _OrdersWrapperClient()
    monkeypatch.setattr(order_entry, "ib_place_order", ib_place_order)
    server_mod.order_owner._pool.client = orders_client
    return orders_client


def _bag(client, attempt):
    from scripts.tests.test_gate4_route import _guaranteed_bag_body, _seed_snapshot, _stock

    engine = _pg_engine()
    _seed_snapshot(engine, [_stock("SPY", 100)])
    engine.dispose()
    return client.post("/orders/place", json=_guaranteed_bag_body(attempt))


def _attempt(attempt):
    return next(r._mapping for r in _submission_rows() if r.client_attempt_id == attempt)


def test_place_runs_on_the_owner_and_stores_the_owner_id(client, fresh_quote, no_spawn_no_connect, monkeypatch):
    orders_client = _real_place_core(monkeypatch, [("status", "Submitted", 555001)])
    resp = _bag(client, "t4-ok")
    assert resp.status_code == 200, resp.text
    assert resp.json()["clientId"] == OWNER_ID
    row = _attempt("t4-ok")
    assert (row["state"], row["perm_id"], row["placing_client_id"]) == ("WORKING", "555001", OWNER_ID)
    assert orders_client.trade.order.orderRef == f"xo:{row['submission_id']}"


def test_real_place_core_rejection_stays_terminal(client, fresh_quote, no_spawn_no_connect, monkeypatch):
    """Astra round 2: the core's explicit outcome=rejected (after the send
    mark) is a proven broker rejection — REJECTED, never UNKNOWN."""
    _real_place_core(monkeypatch, [("error", 7, 201, "Order rejected - reason: margin")])
    resp = _bag(client, "t4-rej")
    assert (resp.status_code, resp.json()["reason_code"]) == (502, "IB_REJECT"), resp.text
    row = _attempt("t4-rej")
    assert (row["state"], row["reason_code"]) == ("REJECTED", "IB_REJECT")


def test_place_sent_without_ack_is_unknown(client, fresh_quote, no_spawn_no_connect, monkeypatch):
    """Sent, then the socket dies in the ack wait: the core says uncertain,
    the owner keeps it unconfirmed → PLACE_OUTCOME_UNKNOWN, row UNKNOWN."""
    orders_client = _real_place_core(monkeypatch, [])

    def _dies(_seconds):
        raise ConnectionError("socket closed mid-ack")

    orders_client.sleep = _dies
    resp = _bag(client, "t4-noack")
    assert (resp.status_code, resp.json()["reason_code"]) == (502, "PLACE_OUTCOME_UNKNOWN"), resp.text
    assert resp.json()["submission_id"] == _attempt("t4-noack")["submission_id"]
    row = _attempt("t4-noack")
    assert (row["state"], row["placing_client_id"]) == ("UNKNOWN", OWNER_ID)


def test_place_deadline_after_start_is_unknown_and_worker_finishes(
    client, fresh_quote, no_spawn_no_connect, monkeypatch
):
    """A3: the request answers PLACE_OUTCOME_UNKNOWN at its deadline; the
    worker still owns the final write (UNKNOWN → WORKING with the owner id)."""
    import time

    from scripts.tests.order_owner_fakes import install_cores

    started, release = threading.Event(), threading.Event()

    def slow_core(orders_client, params):
        started.set()
        orders_client.place_order(None, None)
        release.wait(5)
        return {"status": "ok", "outcome": "accepted", "orderId": 61, "permId": 610001, "clientId": 26}

    install_cores(monkeypatch, place=slow_core)
    monkeypatch.setattr(order_entry, "_PLACE_TIMEOUT_S", 0.3)
    resp = _bag(client, "t4-late")
    assert (resp.status_code, resp.json()["reason_code"]) == (502, "PLACE_OUTCOME_UNKNOWN"), resp.text
    assert _attempt("t4-late")["state"] == "UNKNOWN"
    release.set()
    for _ in range(50):
        if _attempt("t4-late")["state"] != "UNKNOWN":
            break
        time.sleep(0.05)
    row = _attempt("t4-late")
    assert (row["state"], row["perm_id"], row["placing_client_id"]) == ("WORKING", "610001", OWNER_ID)


# -- T4-fix: worker settlement after the request deadline (Astra T4 1-2) ------


def _wait_state(attempt, until, tries=60):
    import time

    for _ in range(tries):
        if _attempt(attempt)["state"] in until:
            break
        time.sleep(0.05)
    return _attempt(attempt)


def _late_uncertain_core(monkeypatch, release, ids, on_late=None):
    """Sends, outlives the request deadline, then reports an uncertain
    result carrying the ids it learned (stand-in clientId 26)."""

    def core(orders_client, params):
        orders_client.place_order(None, None)
        release.wait(5)
        if on_late is not None:
            on_late(params["submission_id"])
        return {"status": "uncertain", "outcome": "uncertain", "message": "ack lost", "clientId": 26, **ids}

    install_cores(monkeypatch, place=core)
    monkeypatch.setattr(order_entry, "_PLACE_TIMEOUT_S", 0.3)


def test_place_late_refusal_after_deadline_is_failed_and_released(
    client, fresh_quote, no_spawn_no_connect, _order_owner, monkeypatch
):
    """Deadline (row UNKNOWN), then the started worker refuses before any
    send: one settlement, zero sends, final FAILED (reservation released)."""
    release = threading.Event()

    def held_probe():
        release.wait(5)
        _order_owner.client.readiness = "unknown"

    _order_owner.client.on_probe = held_probe
    settles = []
    real_finish = order_entry._finish_place
    monkeypatch.setattr(order_entry, "_finish_place", lambda sid, reply: settles.append(reply) or real_finish(sid, reply))
    monkeypatch.setattr(order_entry, "_PLACE_TIMEOUT_S", 0.3)
    resp = _bag(client, "t4f-refuse")
    assert (resp.status_code, resp.json()["reason_code"]) == (502, "PLACE_OUTCOME_UNKNOWN"), resp.text
    assert _attempt("t4f-refuse")["state"] == "UNKNOWN"
    release.set()
    row = _wait_state("t4f-refuse", {"FAILED"})
    assert (row["state"], row["reason_code"]) == ("FAILED", "IB_BROKER_DISCONNECTED")
    assert _order_owner.client.sent == []
    assert len(settles) == 1 and settles[0]["amend_outcome"] == "not_sent"


def test_place_late_uncertain_after_deadline_keeps_ids_and_owner_id(
    client, fresh_quote, no_spawn_no_connect, _order_owner, monkeypatch
):
    """Deadline writes an empty UNKNOWN; the worker's uncertain result then
    enriches it with orderId / permId / the owner clientId and its audit row."""
    release, done = threading.Event(), threading.Event()
    _late_uncertain_core(monkeypatch, release, {"orderId": 62, "permId": 620001})
    real_finish = order_entry._finish_place
    # Settlement done = the UNKNOWN write AND its IB_NOTICE event row.
    monkeypatch.setattr(order_entry, "_finish_place", lambda sid, reply: (real_finish(sid, reply), done.set())[0])
    resp = _bag(client, "t4f-ids")
    assert (resp.status_code, resp.json()["reason_code"]) == (502, "PLACE_OUTCOME_UNKNOWN"), resp.text
    assert _attempt("t4f-ids")["perm_id"] is None
    release.set()
    assert done.wait(5)
    row = _attempt("t4f-ids")
    assert (row["state"], row["ib_order_id"], row["perm_id"], row["placing_client_id"]) == (
        "UNKNOWN",
        "62",
        "620001",
        OWNER_ID,
    )
    engine = _pg_engine()
    with engine.connect() as conn:
        kinds = conn.execute(
            text("SELECT kind FROM xenon.order_events WHERE submission_id = :s"), {"s": row["submission_id"]}
        ).scalars().all()
    engine.dispose()
    assert "IB_NOTICE" in kinds


def test_place_late_uncertain_never_overrides_the_poller(
    client, fresh_quote, no_spawn_no_connect, _order_owner, monkeypatch
):
    """Deadline, then the poller binds and fills the row from broker
    evidence, then the worker's uncertain result lands: FILLED and the
    poller's ids / fill data stay."""
    from scripts.tests.test_gate4_route import _bind_and_fill_like_poller

    release, done = threading.Event(), threading.Event()

    def poller_first(sid):
        _bind_and_fill_like_poller(["", json.dumps({"submission_id": sid})])

    _late_uncertain_core(monkeypatch, release, {"orderId": 63, "permId": 630001}, on_late=poller_first)
    real_finish = order_entry._finish_place
    monkeypatch.setattr(order_entry, "_finish_place", lambda sid, reply: (real_finish(sid, reply), done.set())[0])
    resp = _bag(client, "t4f-poller")
    assert (resp.status_code, resp.json()["reason_code"]) == (502, "PLACE_OUTCOME_UNKNOWN"), resp.text
    release.set()
    assert done.wait(5)
    row = _attempt("t4f-poller")
    assert (row["state"], row["filled_qty"], row["perm_id"], row["ib_order_id"]) == ("FILLED", 1, "777100", "4300")


def test_proven_unsent_never_reports_failed_when_the_row_moved_on(monkeypatch):
    """0 rows updated (stronger evidence owns the row): no terminal 503."""
    calls = []
    monkeypatch.setattr(server_mod.orders_store, "mark_terminal", lambda **kw: calls.append(kw) or 0)
    resp = order_entry._finish_place("sub-moved", order_entry._owner_unavailable_reply())
    assert calls[0]["expected_states"] == ("PENDING", "UNKNOWN")
    assert resp.status_code == 502
    assert json.loads(resp.body)["reason_code"] == "PLACE_OUTCOME_UNKNOWN"


def test_place_abandoned_while_queued_is_failed_with_zero_sends(
    client, fresh_quote, no_spawn_no_connect, _order_owner, monkeypatch
):
    release = threading.Event()
    _order_owner._executor.submit(release.wait, 5)  # another order action holds the worker
    monkeypatch.setattr(order_entry, "_PLACE_TIMEOUT_S", 0.2)
    try:
        resp = _bag(client, "t4-queued")
    finally:
        release.set()
    assert (resp.status_code, resp.json()["reason_code"]) == (503, "IB_CONNECTION"), resp.text
    row = _attempt("t4-queued")
    assert (row["state"], row["reason_code"]) == ("FAILED", "IB_CONNECTION")
    assert _order_owner.client.sent == []


@pytest.mark.parametrize(
    ("refuse", "reason"),
    [("halted", "ORDERS_HALTED"), ("lost", "IB_BROKER_DISCONNECTED"), ("no-owner", "IB_BROKER_DISCONNECTED")],
)
def test_place_refused_by_the_owner_is_failed_with_zero_sends(
    refuse, reason, client, fresh_quote, no_spawn_no_connect, _order_owner, monkeypatch
):
    if refuse == "halted":
        server_mod.order_owner._latch_halt("test")
    elif refuse == "lost":
        _order_owner.client.readiness = "lost"
    else:
        monkeypatch.setattr(server_mod, "order_owner", None)
    resp = _bag(client, "t4-refused")
    assert (resp.status_code, resp.json()["reason_code"]) == (503, reason), resp.text
    assert _attempt("t4-refused")["state"] == "FAILED"
    assert _order_owner.client.sent == []
