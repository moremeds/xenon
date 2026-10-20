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


async def test_busy_role_is_bounded_and_others_still_disconnect(caplog):
    pool, clients, _ = _pool_with_clients()
    release = threading.Event()
    # A started order job still holds the orders worker.
    pool._role_executors["orders"].submit(release.wait, 5)

    t0 = time.monotonic()
    with caplog.at_level(logging.CRITICAL, logger="xenon.api.ib_pool"):
        await pool.disconnect_all(timeout=0.3)
    elapsed = time.monotonic() - t0
    release.set()

    assert elapsed < 2, elapsed  # parallel and bounded, not 0.3 * roles or 5 s
    for role, client in clients.items():
        if role != "orders":
            client.disconnect.assert_called_once_with()
    assert pool._connected["orders"] is False
    assert any("orders" in r.getMessage() and "stale sockets" in r.getMessage() for r in caplog.records)


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
