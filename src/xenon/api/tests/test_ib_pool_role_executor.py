"""Tests for IBPool's per-role single-worker executors (event-loop pinning fix).

After a Gateway bounce + container restart, the activity poller's tick raised
``There is no current event loop in thread 'ThreadPoolExecutor-0_1'`` every
60s. Root cause: ``_connect_in_thread`` creates an event loop on whichever
worker the default ``asyncio.to_thread`` happens to pick; later ticks that
land on a *different* worker have no loop and the ib_async internals fail.

The fix pins all sync IB work for a given role to ONE dedicated worker via
``IBPool.run_sync(role, fn, ...)`` backed by a ``ThreadPoolExecutor(max_workers=1)``.
Connect, reconnect, and every tick share that single worker thread, so the
event loop ``_connect_in_thread`` set up stays current for every later call.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from xenon.api.ib_pool import IBPool


def _make_fake_client(connected: bool = True) -> MagicMock:
    client = MagicMock()
    client.ib.isConnected.return_value = connected
    return client


@pytest.mark.asyncio
async def test_run_sync_pins_to_named_role_thread():
    """run_sync('sync', fn) must execute fn on a thread whose name marks the role."""
    pool = IBPool()
    captured: dict = {}

    def sync_fn():
        captured["thread_name"] = threading.current_thread().name
        return "ok"

    result = await pool.run_sync("sync", sync_fn)

    assert result == "ok"
    assert "ib_pool_sync" in captured["thread_name"], (
        f"sync work ran on {captured['thread_name']!r} — not a role-pinned thread; "
        f"event-loop-per-thread fix is bypassed"
    )


@pytest.mark.asyncio
async def test_run_sync_reuses_same_worker_thread_across_calls():
    """All run_sync('sync', ...) calls hit the same worker so the loop stays current."""
    pool = IBPool()
    seen_thread_ids: set = set()

    def sync_fn():
        seen_thread_ids.add(threading.get_ident())

    for _ in range(5):
        await pool.run_sync("sync", sync_fn)

    assert len(seen_thread_ids) == 1, (
        f"role-pinned executor must reuse one worker; saw {len(seen_thread_ids)} threads"
    )


@pytest.mark.asyncio
async def test_run_sync_isolates_roles():
    """Each role's executor is its own worker — sync and data must not share."""
    pool = IBPool()
    threads: dict = {}

    def sync_fn(role: str):
        threads[role] = threading.get_ident()

    await pool.run_sync("sync", sync_fn, "sync")
    await pool.run_sync("data", sync_fn, "data")
    await pool.run_sync("orders", sync_fn, "orders")

    assert threads["sync"] != threads["data"]
    assert threads["sync"] != threads["orders"]
    assert threads["data"] != threads["orders"]


@pytest.mark.asyncio
async def test_run_sync_passes_args_and_kwargs():
    """The runner must forward args and kwargs to the sync callable verbatim."""
    pool = IBPool()

    def sync_fn(a, b, *, c, d):
        return (a, b, c, d)

    result = await pool.run_sync("sync", sync_fn, 1, 2, c=3, d=4)
    assert result == (1, 2, 3, 4)


@pytest.mark.asyncio
async def test_run_sync_propagates_exceptions():
    """Exceptions from the sync callable must surface as awaited exceptions."""
    pool = IBPool()

    def sync_fn():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await pool.run_sync("sync", sync_fn)


@pytest.mark.asyncio
async def test_run_sync_rejects_unknown_role():
    """Programmer error: invalid role must raise ValueError, not silently spawn."""
    pool = IBPool()
    with pytest.raises(ValueError):
        await pool.run_sync("nonexistent", lambda: None)


@pytest.mark.asyncio
async def test_connect_all_uses_role_executor(monkeypatch):
    """connect_all must run _connect_in_thread on the role's pinned worker so the
    loop it creates lands on the SAME thread later run_sync calls will use."""
    pool = IBPool()
    connect_thread_names: list = []

    def fake_connect(host, port, client_id, timeout):
        connect_thread_names.append(threading.current_thread().name)
        client = _make_fake_client(connected=True)
        return client

    monkeypatch.setattr("xenon.api.ib_pool._connect_in_thread", fake_connect)

    await pool.connect_all()

    # One connect per role, each on its role-pinned thread.
    assert len(connect_thread_names) == 3
    for name in connect_thread_names:
        assert "ib_pool_" in name, f"connect ran on {name!r}, expected a role-pinned thread"


@pytest.mark.asyncio
async def test_run_sync_after_connect_runs_on_same_thread(monkeypatch):
    """Critical correctness invariant: connect AND every later sync op for a role
    run on the SAME worker thread. This is the whole point of the fix."""
    pool = IBPool()
    threads_used: list = []

    def fake_connect(host, port, client_id, timeout):
        threads_used.append(("connect", threading.get_ident()))
        return _make_fake_client(connected=True)

    monkeypatch.setattr("xenon.api.ib_pool._connect_in_thread", fake_connect)

    await pool.connect_all()

    def tick_fn():
        threads_used.append(("tick", threading.get_ident()))

    await pool.run_sync("sync", tick_fn)
    await pool.run_sync("sync", tick_fn)

    sync_connect_tid = next(tid for label, tid in threads_used if label == "connect")
    sync_tick_tids = [tid for label, tid in threads_used if label == "tick"]
    # The first connect was for 'sync' (POOL_ROLES is dict-ordered).
    # All tick calls on 'sync' role must hit the same thread.
    assert all(tid == sync_connect_tid for tid in sync_tick_tids), (
        "sync-role ticks must run on the same worker as the sync-role connect — "
        "if they don't, the event loop _connect_in_thread set up is unreachable"
    )


@pytest.mark.asyncio
async def test_disconnect_all_then_connect_all_succeeds(monkeypatch):
    """The /ib/restart endpoint and the auto-restart wrapper both call
    disconnect_all() then connect_all() on the SAME pool instance. If
    disconnect_all leaves executors in a shut-down state, the next
    connect_all hits ``RuntimeError: cannot schedule new futures after
    shutdown`` for every role — silently reporting the pool dead.
    """
    pool = IBPool()

    def fake_connect(host, port, client_id, timeout):
        return _make_fake_client(connected=True)

    monkeypatch.setattr("xenon.api.ib_pool._connect_in_thread", fake_connect)

    # First connect — populates clients, executors are fresh.
    status1 = await pool.connect_all()
    assert all(status1.values()), f"first connect_all failed: {status1}"

    # Disconnect — must NOT permanently kill the executors.
    await pool.disconnect_all()

    # Re-connect on the same pool instance — must succeed.
    status2 = await pool.connect_all()
    assert all(status2.values()), (
        f"second connect_all failed: {status2} — "
        f"disconnect_all left executors unusable"
    )

    # And run_sync on a re-created executor must work.
    captured: dict = {}

    def sync_fn():
        captured["thread_name"] = threading.current_thread().name

    await pool.run_sync("sync", sync_fn)
    assert "ib_pool_sync" in captured["thread_name"]


def test_server_factories_pass_role_runner_to_pool(monkeypatch):
    """Regression: rehydrate, fills replay, and activity poller must dispatch
    sync work via ib_pool.run_sync (not bare asyncio.to_thread). Source inspect
    mirrors the precedent at scripts/tests/test_replay_unknown_orders.py:154."""
    import inspect

    from xenon.api import server as server_mod

    rehydrate_src = inspect.getsource(server_mod._run_rehydrate_on_boot)
    fills_src = inspect.getsource(server_mod._run_fills_replay_on_boot)
    poller_src = inspect.getsource(server_mod._maybe_start_activity_poller)

    assert "ib_pool.run_sync" in rehydrate_src, (
        "_run_rehydrate_on_boot must dispatch via ib_pool.run_sync('sync', ...) so "
        "the rehydrate tick runs on the role-pinned worker"
    )
    assert "ib_pool.run_sync" in fills_src, (
        "_run_fills_replay_on_boot must dispatch via ib_pool.run_sync('sync', ...)"
    )
    assert "ib_pool.run_sync" in poller_src, (
        "_maybe_start_activity_poller must wire an async_runner backed by "
        "ib_pool.run_sync('sync', ...) into activity_poller_loop"
    )


def test_acquire_callers_dispatch_via_run_sync_not_to_thread():
    """Regression: every ``async with pool.acquire(role)`` block must dispatch
    its IB work through ``pool.run_sync(role, ...)``, not bare
    ``asyncio.to_thread``. Otherwise the IB call runs on the default executor
    pool and misses the role's pinned event loop — same bug class as the one
    this PR fixes for the sync role's boot/poller path.

    Source-string check; brittle on rename but matches the
    `test_replay_unknown_orders.py:154` precedent. A behavioral test would
    require booting the full async surface for each route.
    """
    import inspect

    from xenon.api import server as server_mod
    from xenon.api.routes import historical as historical_mod
    from xenon.api.routes import wizard as wizard_mod

    # Each entry: (callable, expected role for run_sync dispatch)
    targets = [
        (server_mod._fetch_ib_expiry_candidates, "data"),
        (server_mod._fetch_quote_snapshot, "data"),
        (server_mod._fetch_order_quote_snapshot, "data"),
        (server_mod._qualify_order_con_id, "data"),
        (historical_mod.qualify_contracts, "data"),
        (historical_mod.head_timestamp, "data"),
        (historical_mod.historical_bars, "data"),
        (wizard_mod.wizard_protect, "orders"),
    ]
    for fn, role in targets:
        source = inspect.getsource(fn)
        assert "run_sync" in source, (
            f"{fn.__qualname__} must dispatch IB work via "
            f"pool.run_sync('{role}', ...) so the call runs on the role-pinned "
            f"worker thread (not the default asyncio.to_thread executor)"
        )
        # Confirm the bare to_thread pattern on a client method is gone in
        # this function. We allow asyncio.to_thread elsewhere — only flag
        # patterns clearly using the pool client.
        forbidden_patterns = [
            "asyncio.to_thread(client.",
            "asyncio.to_thread(ib_client.",
            "asyncio.to_thread(\n                client.",
            "asyncio.to_thread(\n                ib_client.",
        ]
        for pat in forbidden_patterns:
            assert pat not in source, (
                f"{fn.__qualname__} still uses bare asyncio.to_thread on a "
                f"pool client — that bypasses the role-pinned executor and "
                f"resurrects the event-loop bug (matched: {pat!r})"
            )
