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
