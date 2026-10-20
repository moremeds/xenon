# ruff: noqa: F811 -- route fixtures are imported by name and re-bound as test args
"""PR-10 T3 — /orders/cancel and /orders/modify on the single order owner.

Positive routing (A1), owner path spawns nothing and connects nothing,
worker-owned Gate 4 settlement (v3 item 4 / A3 amend rules). Real route +
Gate 4 + Postgres; only the broker boundary is faked."""

from __future__ import annotations

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
    assert _order_owner.client.ready_checks == 1  # fresh round trip right before the send


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
    monkeypatch.setattr(server_mod, "_ORDER_ACTION_TIMEOUT_S", 0.2)
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
    monkeypatch.setattr(server_mod, "_ORDER_ACTION_TIMEOUT_S", 0.1)
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
