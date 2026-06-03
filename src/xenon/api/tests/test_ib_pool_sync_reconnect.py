"""Tests for ``IBPool.get_with_reconnect_sync`` — sync reconnect path.

The pool's async ``acquire()`` context manager auto-reconnects a dropped
role; ``get()`` is a plain dict lookup. Sync callers running inside
``asyncio.to_thread`` (boot rehydrate, fills replay, activity poller)
were reaching for ``get()`` because no sync sibling existed — so when IB
Gateway bounced, the client they got back was disconnected and every
tick raised ``Not connected to IB`` until the FastAPI container was
manually restarted.

These tests pin the contract of the new sync helper that closes that gap.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from xenon.api.ib_pool import IBPool


def _make_fake_client(connected: bool = True) -> MagicMock:
    """Build a fake IBClient stand-in with toggleable isConnected()."""
    client = MagicMock()
    client.ib.isConnected.return_value = connected
    return client


@pytest.fixture
def pool_with_disconnected_sync(monkeypatch) -> IBPool:
    """Pool whose 'sync' role is populated but reports isConnected()=False."""
    pool = IBPool()
    fake = _make_fake_client(connected=False)
    pool._clients["sync"] = fake
    pool._connected["sync"] = True  # the pool's internal flag is stale
    return pool


@pytest.fixture
def pool_with_connected_sync() -> IBPool:
    pool = IBPool()
    fake = _make_fake_client(connected=True)
    pool._clients["sync"] = fake
    pool._connected["sync"] = True
    return pool


def test_returns_client_when_already_connected(pool_with_connected_sync, monkeypatch):
    """Happy path: connected role short-circuits, no reconnect attempt."""
    connect_calls: list = []

    def fake_connect(*args, **kwargs):
        connect_calls.append((args, kwargs))
        return _make_fake_client(connected=True)

    monkeypatch.setattr("xenon.api.ib_pool._connect_in_thread", fake_connect)

    client = pool_with_connected_sync.get_with_reconnect_sync("sync")

    assert client is pool_with_connected_sync._clients["sync"]
    assert connect_calls == []


def test_reconnects_when_stale_and_returns_new_client(pool_with_disconnected_sync, monkeypatch):
    """Disconnected role triggers a sync reconnect; pool publishes new client."""
    fresh_client = _make_fake_client(connected=True)
    captured: dict = {}

    def fake_connect(host, port, client_id, timeout):
        captured["args"] = (host, port, client_id, timeout)
        return fresh_client

    monkeypatch.setattr("xenon.api.ib_pool._connect_in_thread", fake_connect)

    returned = pool_with_disconnected_sync.get_with_reconnect_sync("sync")

    assert returned is fresh_client
    assert pool_with_disconnected_sync._clients["sync"] is fresh_client
    assert pool_with_disconnected_sync._connected["sync"] is True
    # Pool was constructed with default host/port and POOL_ROLES["sync"]=3.
    assert captured["args"][2] == 3


def test_raises_connection_error_when_reconnect_fails(pool_with_disconnected_sync, monkeypatch):
    """A failed reconnect surfaces ConnectionError, not the underlying exc."""
    def fake_connect(*args, **kwargs):
        raise OSError("gateway unreachable")

    monkeypatch.setattr("xenon.api.ib_pool._connect_in_thread", fake_connect)

    with pytest.raises(ConnectionError) as excinfo:
        pool_with_disconnected_sync.get_with_reconnect_sync("sync")

    assert "sync" in str(excinfo.value)
    assert pool_with_disconnected_sync._connected["sync"] is False


def test_concurrent_callers_share_one_reconnect_attempt(pool_with_disconnected_sync, monkeypatch):
    """Two threads racing on a dead role must coalesce to one reconnect.

    Otherwise both call ``_connect_in_thread(client_id=3)`` simultaneously,
    racing on the same IB clientId slot at the Gateway.
    """
    connect_calls: list = []
    fresh_client = _make_fake_client(connected=True)
    in_connect = threading.Event()
    release_connect = threading.Event()

    def fake_connect(host, port, client_id, timeout):
        connect_calls.append(client_id)
        in_connect.set()
        # Hold the connect open until we've confirmed the second caller is waiting.
        release_connect.wait(timeout=2.0)
        return fresh_client

    monkeypatch.setattr("xenon.api.ib_pool._connect_in_thread", fake_connect)

    results: list = []
    errors: list = []

    def caller():
        try:
            results.append(pool_with_disconnected_sync.get_with_reconnect_sync("sync"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=caller)
    t2 = threading.Thread(target=caller)
    t1.start()
    # Give t1 a head start so it enters the reconnect lock first.
    assert in_connect.wait(timeout=1.0), "first thread never entered connect"
    t2.start()
    # Brief settle: t2 should now be blocked on the reconnect lock or post-lock recheck.
    time.sleep(0.1)
    release_connect.set()

    t1.join(timeout=2.0)
    t2.join(timeout=2.0)

    assert not errors, f"caller threads errored: {errors}"
    assert len(connect_calls) == 1, f"expected 1 reconnect, got {len(connect_calls)}"
    assert all(r is fresh_client for r in results)


def test_unknown_role_raises_value_error():
    """Mirrors ``get()`` / ``acquire()`` — unknown role is a programmer error."""
    pool = IBPool()
    with pytest.raises(ValueError):
        pool.get_with_reconnect_sync("nonexistent")


def test_server_factories_use_reconnect_helper_not_raw_get():
    """Regression: the 3 server.py factories must reach for the sync helper.

    Mirrors ``scripts/tests/test_replay_unknown_orders.py:154`` — the only
    way a sync caller running in ``asyncio.to_thread`` recovers from an IB
    Gateway bounce without a container restart is by going through
    ``get_with_reconnect_sync``. ``_get_managed_account_for_health`` keeps
    raw ``get()`` on purpose (status-read, must not block on reconnect).
    """
    import inspect

    from xenon.api import server as server_mod

    for fn in (
        server_mod._run_rehydrate_on_boot,
        server_mod._maybe_start_activity_poller,
        server_mod._run_fills_replay_on_boot,
    ):
        source = inspect.getsource(fn)
        assert "get_with_reconnect_sync" in source, (
            f"{fn.__name__} must use ib_pool.get_with_reconnect_sync('sync') "
            f"so it recovers from a Gateway bounce without a container restart"
        )
        assert 'ib_pool.get("sync")' not in source, (
            f"{fn.__name__} still calls ib_pool.get('sync') — that is the "
            f"bypass that needs to go away"
        )
