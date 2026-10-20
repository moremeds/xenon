"""Test doubles for the single order owner (PR-10). Fakes only — no IB.

``FakeOrdersClient`` / ``FakePool`` stand in for the pool ``orders``
connection; ``install_owner`` puts a real ``OrderOwner`` over them on
``server.order_owner``.

``install_runner_cores`` replaces the three connected-client cores the
server calls (place, cancel, modify) with fakes that answer through
``server._run_ib_script_with_recovery`` — looked up at call time, so a test
that patches that runner drives every order action the way the pre-PR-10
route tests did. The runner is data-only in production; the entry names
``xenon-ib-place-order`` / ``xenon-ib-order-manage`` and the argument lists
are test LABELS only (those CLIs no longer exist), kept so the existing
suites keep asserting payloads and identity tuples. Nothing is spawned.
``ScriptResult`` maps to the owner lifecycle:

- ``ok=True``                  → the reply dict (sent first unless the reply
                                 proves nothing was sent);
- ``ok=False, spawned=False``  → raise before the send (proven unsent);
- ``ok=False`` otherwise       → send, then raise (unconfirmed).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from xenon.api import server as server_mod
from xenon.api.order_owner import OrderOwner
from xenon.clients.ib_client import READINESS_READY, pool_client_id
from xenon.execution import ib_order_manage, ib_place_order

def orders_id() -> int:
    return pool_client_id("orders")


class FakeOrdersClient:
    def __init__(self) -> None:
        self.readiness = READINESS_READY
        self.sent: list[tuple] = []
        self.ready_checks = 0
        self.generation = 0  # bump to simulate a readiness event (1100/2110/1101)
        self.on_probe = None  # runs inside the open-orders round trip
        self.ib = SimpleNamespace(client=SimpleNamespace(clientId=orders_id()), sleep=lambda _s: None)

    @property
    def broker_readiness(self) -> str:
        return self.readiness

    @property
    def readiness_generation(self) -> int:
        return self.generation

    def confirm_broker_ready(self, timeout: float = 5.0) -> str:
        self.ready_checks += 1
        if self.on_probe is not None:
            self.on_probe()
        return self.readiness

    def place_order(self, *args, **kwargs):
        self.sent.append(("place", args))
        return SimpleNamespace(order=SimpleNamespace(orderId=0, permId=0))

    def cancel_order(self, *args, **kwargs):
        self.sent.append(("cancel", args))

    def disconnect(self) -> None:
        pass


class FakePool:
    """Pool stand-in. ``run_sync`` runs on one real worker thread per pool so
    queue/abandon tests see true ordering; ``connected`` per role."""

    def __init__(self, client: FakeOrdersClient | None = None) -> None:
        import concurrent.futures

        self.client = client or FakeOrdersClient()
        self.connected = {"sync": True, "orders": True, "data": True}
        self.reconnects: list[str] = []
        self.reconnect_error: BaseException | None = None
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="fake_orders")
        self._host, self._port = "127.0.0.1", 4002

    def get(self, role: str):
        # Like IBPool.get: the cached client stays until a replacement.
        return self.client if role == "orders" else None

    def is_connected(self, role: str) -> bool:
        return self.connected[role]

    def broker_readiness(self, role: str) -> str:
        return self.client.readiness if self.connected[role] else "unknown"

    async def run_sync(self, role: str, fn, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, lambda: fn(*args, **kwargs))

    async def reconnect_role(self, role: str) -> None:
        self.reconnects.append(role)
        if self.reconnect_error is not None:
            raise self.reconnect_error
        self.connected[role] = True


def install_owner(monkeypatch, pool: FakePool | None = None) -> tuple[OrderOwner, FakePool]:
    pool = pool or FakePool()
    owner = OrderOwner(pool, port_listening=lambda: True)
    monkeypatch.setattr(server_mod, "order_owner", owner)
    return owner, pool


class _Unsent(Exception):
    pass


def _legacy_args(op: str, order_id, perm_id, kw: dict) -> list[str]:
    args = [op]
    if order_id:
        args += ["--order-id", str(order_id)]
    if perm_id:
        args += ["--perm-id", str(perm_id)]
    if op == "modify":
        if kw.get("new_price") is not None:
            args += ["--new-price", str(kw["new_price"])]
        if kw.get("new_quantity") is not None:
            args += ["--new-quantity", str(kw["new_quantity"])]
        if kw.get("outside_rth") is True:
            args.append("--outside-rth")
        elif kw.get("outside_rth") is False:
            args.append("--no-outside-rth")
    if kw.get("account"):
        args += ["--account", kw["account"]]
    if kw.get("client_id") is not None:
        args += ["--client-id", str(kw["client_id"])]
    if kw.get("non_guaranteed") is not None:
        args += ["--non-guaranteed", str(kw["non_guaranteed"])]
    if kw.get("send_by") is not None:
        args += ["--send-by", kw["send_by"].isoformat()]
    return args


def _answer(client, op: str, args: list[str]) -> dict:
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            server_mod._run_ib_script_with_recovery("xenon-ib-order-manage", args, timeout=15)
        )
    finally:
        loop.close()
    send = client.cancel_order if op == "cancel" else client.place_order
    if not result.ok:
        if not getattr(result, "spawned", True):
            raise _Unsent(result.error or "refused before send")
        send(None)
        raise RuntimeError(result.error or "order action died after send")
    data = dict(result.data or {})
    if data.get("status") == "ok" or data.get("amend_outcome") in ("unconfirmed", "rejected"):
        send(None)
    return data


def _answer_place(client, params: dict) -> dict:
    """Place core double: same send semantics as ``place_order_on_client``.

    A canned ``status: error`` without ``outcome`` / ``permId`` is a pre-send
    error (the real core returns it before ``placeOrder``); anything else is
    sent first. An explicit ``outcome: rejected`` carries
    ``amend_outcome: rejected`` exactly like the real core.
    """
    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            server_mod._run_ib_script_with_recovery("xenon-ib-place-order", ["--json", json.dumps(params)], timeout=30)
        )
    finally:
        loop.close()
    if not result.ok:
        if not getattr(result, "spawned", True):
            raise _Unsent(result.error or "refused before send")
        client.place_order(None, None)
        raise RuntimeError(result.error or "place died after send")
    data = dict(result.data or {})
    if data.get("status") == "error" and not data.get("outcome") and not data.get("permId"):
        return data
    client.place_order(None, None)
    if data.get("outcome") == "rejected":
        data.setdefault("amend_outcome", "rejected")
    return data


def install_runner_cores(monkeypatch, pool: FakePool | None = None) -> tuple[OrderOwner, FakePool]:
    """Owner over a fake pool + cores answered by the patched runner."""

    def cancel(client, order_id, perm_id, **kw):
        return _answer(client, "cancel", _legacy_args("cancel", order_id, perm_id, kw))

    def modify(client, order_id, perm_id, new_price, **kw):
        return _answer(client, "modify", _legacy_args("modify", order_id, perm_id, {"new_price": new_price, **kw}))

    install_cores(monkeypatch, cancel=cancel, modify=modify, place=_answer_place)
    return install_owner(monkeypatch, pool)


def install_cores(monkeypatch, *, cancel=None, modify=None, place=None) -> None:
    """Swap the cores the SERVER calls; the real modules stay untouched."""
    proxy = SimpleNamespace(
        cancel_order_on_client=cancel or ib_order_manage.cancel_order_on_client,
        modify_order_on_client=modify or ib_order_manage.modify_order_on_client,
    )
    monkeypatch.setattr(server_mod, "ib_order_manage", proxy)
    monkeypatch.setattr(
        server_mod,
        "ib_place_order",
        SimpleNamespace(place_order_on_client=place or ib_place_order.place_order_on_client),
    )
