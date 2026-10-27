"""Regression: a refused/hung Futu OpenD must never freeze xenon-api's event loop.

2026-10 incident: the 16:30 ET futu_history_loop called scope_factory() ->
FutuClient.connect() synchronously on the event loop. OpenD refused the
container's connection and the futu SDK retried forever, so every HTTP route
(incl. /options/greeks) hung ~16:20 -> ~20:25 ET each weekday until the
Colima watchdog restarted the VM.
"""

from __future__ import annotations

import asyncio
import socket
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from xenon.clients.futu_client import FutuClient
from xenon.clients.futu_exceptions import FutuConnectionError
from xenon.execution.account_scope import AccountScope


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_connect_refused_fails_fast_without_opening_sdk_context():
    with patch("xenon.clients.futu_client.OpenSecTradeContext") as ctx_cls:
        client = FutuClient(host="127.0.0.1", port=_closed_port())
        started = time.monotonic()
        with pytest.raises(FutuConnectionError):
            client.connect()
    assert time.monotonic() - started < 5
    ctx_cls.assert_not_called()  # the SDK context is what retries forever
    assert not client.is_connected()


@pytest.mark.asyncio
async def test_hanging_scope_factory_does_not_block_event_loop(monkeypatch):
    import xenon.api.services.futu_history_scheduler as sched_mod

    real_sleep = asyncio.sleep

    async def _instant_sleep(seconds):
        return await real_sleep(0)

    monkeypatch.setattr(sched_mod.asyncio, "sleep", _instant_sleep)
    monkeypatch.setattr(sched_mod, "SCOPE_TIMEOUT_S", 0.2)

    errored = asyncio.Event()

    def _capture(service, state="ok", **kwargs):
        if state == "error":
            errored.set()

    monkeypatch.setattr(sched_mod, "record_service_health", _capture)

    def _hanging_scope_factory():
        time.sleep(2)  # stands in for the SDK's forever-retry connect

    def _engine_factory():
        eng = MagicMock()
        eng.dispose = AsyncMock()
        return eng

    runner = AsyncMock()
    started = time.monotonic()
    task = asyncio.create_task(
        sched_mod.futu_history_loop(
            engine_factory=_engine_factory,
            scopes_factory=_hanging_scope_factory,
            runner=runner,
        )
    )
    await asyncio.wait_for(errored.wait(), timeout=5.0)
    elapsed = time.monotonic() - started
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Inline, the 2 s blocking call would freeze the loop for >= 2 s.
    assert elapsed < 1.0
    runner.assert_not_called()


@pytest.mark.asyncio
async def test_blocking_runner_does_not_block_event_loop(monkeypatch):
    """The sync itself (SDK connect, fetches, throttle sleeps) runs off the API loop."""
    import xenon.api.services.futu_history_scheduler as sched_mod

    real_sleep = asyncio.sleep

    async def _instant_sleep(seconds):
        return await real_sleep(0)

    monkeypatch.setattr(sched_mod.asyncio, "sleep", _instant_sleep)
    done = asyncio.Event()
    monkeypatch.setattr(sched_mod, "record_service_health", lambda *a, **k: done.set())

    async def _blocking_runner(engine, scope, since):
        time.sleep(1.0)  # stands in for FutuClient's synchronous calls
        return {}

    def _engine_factory():
        eng = MagicMock()
        eng.dispose = AsyncMock()
        return eng

    scope = AccountScope(broker="FUTU", account_env="live", broker_account="pytest")
    task = asyncio.create_task(
        sched_mod.futu_history_loop(
            engine_factory=_engine_factory, scopes_factory=lambda: [scope], runner=_blocking_runner
        )
    )
    ticks = 0
    while not done.is_set():
        before = time.monotonic()
        await real_sleep(0.05)
        assert time.monotonic() - before < 0.5, "API event loop stalled behind the Futu sync"
        ticks += 1
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert ticks >= 5
