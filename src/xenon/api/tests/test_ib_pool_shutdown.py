"""PR-10 T5: graceful IB disconnect on shutdown.

A socket left open past the container stop keeps its clientId in use on the
gateway (seen after a Watchtower redeploy via colima usernet), and the next
api start halts orders on 326. ``IBPool.disconnect_all`` therefore
disconnects every role in parallel on its own worker, bounded per role, and
never raises.
"""

from __future__ import annotations

import logging
import threading
import time
from unittest.mock import MagicMock

import pytest

from xenon.api.ib_pool import POOL_ROLES, IBPool

pytestmark = pytest.mark.asyncio


def _pool_with_clients() -> tuple[IBPool, dict[str, MagicMock], dict[str, str]]:
    pool = IBPool()
    threads: dict[str, str] = {}
    clients: dict[str, MagicMock] = {}
    for role in POOL_ROLES:
        client = MagicMock()

        def _disconnect(role=role):
            threads[role] = threading.current_thread().name

        client.disconnect.side_effect = _disconnect
        clients[role] = client
        pool._clients[role] = client
        pool._connected[role] = True
    return pool, clients, threads


async def test_every_role_disconnects_on_its_own_worker():
    pool, clients, threads = _pool_with_clients()

    await pool.disconnect_all(timeout=5)

    for role, client in clients.items():
        client.disconnect.assert_called_once_with()
        assert threads[role].startswith(f"ib_pool_{role}")
        assert pool._connected[role] is False
    assert pool._clients == {}
    assert all(ex._shutdown for ex in pool._role_executors.values())


@pytest.mark.parametrize("release_after_wait", [False, True], ids=["released-before-deadline", "released-after-deadline"])
async def test_busy_role_disconnect_is_never_dropped(release_after_wait, caplog):
    """Astra T5 F1: a disconnect queued behind a started order job is never
    cancelled or forgotten — it runs once when the job ends, whether that is
    before or after disconnect_all stops waiting."""
    pool, clients, _ = _pool_with_clients()
    release = threading.Event()
    busy = pool._role_executors["orders"].submit(release.wait, 5)  # a started order job
    if not release_after_wait:
        threading.Timer(0.1, release.set).start()

    t0 = time.monotonic()
    with caplog.at_level(logging.CRITICAL, logger="xenon.ib_pool"):
        await pool.disconnect_all(timeout=0.5 if not release_after_wait else 0.2)
    elapsed = time.monotonic() - t0
    assert elapsed < 2, elapsed  # parallel and bounded, not per role
    for role, client in clients.items():
        if role != "orders":
            client.disconnect.assert_called_once_with()

    if release_after_wait:
        clients["orders"].disconnect.assert_not_called()
        assert pool._clients["orders"] is clients["orders"]  # still reachable
        assert any("still queued" in r.getMessage() for r in caplog.records)
        release.set()
    busy.result(timeout=5)
    for _ in range(100):
        if clients["orders"].disconnect.call_count:
            break
        time.sleep(0.01)
    assert clients["orders"].disconnect.call_count == 1


async def test_shutdown_during_reconnect_closes_the_late_connection(monkeypatch):
    """Astra T5 F2: SIGTERM while the owner supervisor awaits reconnect_role.
    The supervisor is cancelled, the connect finishes after disconnect_all
    began: the worker disconnects the new client and never registers it."""
    import asyncio

    from xenon.api import ib_pool as ib_pool_mod

    pool = IBPool()
    connecting, release = threading.Event(), threading.Event()
    late = MagicMock()

    def held_connect(host, port, client_id, timeout):
        connecting.set()
        release.wait(5)
        return late

    monkeypatch.setattr(ib_pool_mod, "_connect_in_thread", held_connect)
    supervisor = asyncio.create_task(pool.reconnect_role("orders"))
    while not connecting.is_set():
        await asyncio.sleep(0.01)
    supervisor.cancel()  # OrderOwner.stop() cancels the supervisor mid-await
    await pool.disconnect_all(timeout=0.2)  # nothing registered to disconnect yet
    release.set()
    for _ in range(200):
        if late.disconnect.call_count:
            break
        await asyncio.sleep(0.01)
    assert late.disconnect.call_count == 1
    assert "orders" not in pool._clients
    with pytest.raises(asyncio.CancelledError):
        await supervisor


async def test_disconnect_error_never_escapes():
    pool, clients, _ = _pool_with_clients()
    clients["sync"].disconnect.side_effect = RuntimeError("socket already gone")

    await pool.disconnect_all(timeout=5)

    for role, client in clients.items():
        client.disconnect.assert_called_once_with()
    assert pool._clients == {}


async def test_subset_keeps_executors_and_other_roles():
    pool, clients, _ = _pool_with_clients()

    await pool.disconnect_all(roles=("sync", "data"))

    clients["orders"].disconnect.assert_not_called()
    assert "orders" in pool._clients
    assert not any(ex._shutdown for ex in pool._role_executors.values())
    await pool.disconnect_all(timeout=5)


@pytest.mark.parametrize(
    "scenario",
    ["connect-first-worker-done-before-close-wait", "connect-first-worker-done-after-close-wait", "close-first"],
)
async def test_full_connect_overlapping_full_shutdown_never_orphans(scenario, monkeypatch):
    """Astra T5 R2-1: a full connect overlapping a full shutdown. Every client
    created is either registered in the live pool or disconnected."""
    import asyncio

    from xenon.api import ib_pool as ib_pool_mod

    pool = IBPool()
    created: list[MagicMock] = []
    connecting, release = threading.Event(), threading.Event()

    def held_connect(host, port, client_id, timeout):
        connecting.set()
        release.wait(5)
        client = MagicMock()
        created.append(client)
        return client

    monkeypatch.setattr(ib_pool_mod, "_connect_in_thread", held_connect)

    if scenario == "close-first":
        release.set()
        await pool.connect_all()  # pool open with every role
        busy_release = threading.Event()
        pool._role_executors["orders"].submit(busy_release.wait, 5)  # close keeps the lock
        close = asyncio.create_task(pool.disconnect_all(timeout=None))
        await asyncio.sleep(0.05)
        reopen = asyncio.create_task(pool.connect_all())  # must wait for the close
        await asyncio.sleep(0.05)
        assert not reopen.done()
        busy_release.set()
        await close
        await reopen
    else:
        connect = asyncio.create_task(pool.connect_all())
        while not connecting.is_set():
            await asyncio.sleep(0.01)
        if scenario == "connect-first-worker-done-before-close-wait":
            threading.Timer(0.1, release.set).start()
            await pool.disconnect_all(timeout=2)
        else:
            await pool.disconnect_all(timeout=0.2)  # lock wait expires first
            release.set()
        status = await connect
        assert not any(status.values())  # no connect survives the close
        assert len(created) == 1  # the retry / next-role loops stopped

    def _owned(client) -> bool:
        # Registered in a LIVE pool (its role worker still runs) or disconnected.
        roles = [r for r, c in pool._clients.items() if c is client]
        live = bool(roles) and not pool._role_executors[roles[0]]._shutdown
        return live or client.disconnect.call_count == 1

    for _ in range(300):  # worker-side connects / disconnects may still be draining
        if all(_owned(c) for c in created):
            break
        await asyncio.sleep(0.01)
    assert created and all(_owned(c) for c in created), scenario
    if scenario != "close-first":
        assert pool._clients == {}
    for executor in list(pool._role_executors.values()):
        executor.shutdown(wait=True)


async def test_connect_worker_outliving_close_and_reopen_never_registers(monkeypatch):
    """Astra T5 R3-1: a cancelled full connect leaves its worker running; a
    close and a reopen follow; the old worker then returns. Its client (old
    generation) is disconnected, and the reopened pool keeps its own client."""
    import asyncio

    from xenon.api import ib_pool as ib_pool_mod

    pool = IBPool()
    first_started, release_first = threading.Event(), threading.Event()
    created: list[MagicMock] = []

    def connect(host, port, client_id, timeout):
        client = MagicMock()
        if not created:  # the first connect (old generation) is held
            created.append(client)
            first_started.set()
            release_first.wait(5)
            return client
        created.append(client)
        return client

    monkeypatch.setattr(ib_pool_mod, "_connect_in_thread", connect)

    first_role = next(iter(POOL_ROLES))
    task = asyncio.create_task(pool.connect_all())
    while not first_started.is_set():
        await asyncio.sleep(0.01)
    old_executor = pool._role_executors[first_role]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await pool.disconnect_all(timeout=0.01)  # nothing registered yet

    await pool.connect_all()  # reopen: new executors, new clients
    new_client = pool._clients[first_role]
    assert new_client is not created[0]

    release_first.set()  # the old worker returns after the reopen
    old_executor.shutdown(wait=True)

    old_client = created[0]
    assert pool._clients[first_role] is new_client
    assert old_client.disconnect.call_count == 1
    assert new_client.disconnect.call_count == 0

    await pool.disconnect_all(timeout=5)
    for executor in list(pool._role_executors.values()):
        executor.shutdown(wait=True)
