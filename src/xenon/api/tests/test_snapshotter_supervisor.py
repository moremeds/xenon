"""Unit tests for the option-chain snapshotter supervisor restart loop.

The snapshotter runs in-process under the FastAPI lifespan, so nothing external
restarts it. `_snapshotter_supervise_loop` must:
  - restart the run-coroutine after a non-cancellation crash (self-heal),
  - propagate CancelledError on shutdown (not swallow it),
  - stop (not hot-loop) if the run-coroutine returns cleanly.
"""

from __future__ import annotations

import asyncio

import pytest

from xenon.api.server import _snapshotter_supervise_loop


@pytest.mark.asyncio
async def test_retries_after_crash_then_keeps_running():
    calls: list[int] = []

    async def factory():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("boom")  # crash twice
        await asyncio.sleep(10)  # then "run forever"

    task = asyncio.ensure_future(_snapshotter_supervise_loop(factory, backoff_s=0.01))
    await asyncio.sleep(0.15)

    assert len(calls) >= 3, f"supervisor did not restart past crashes (calls={len(calls)})"
    assert not task.done(), "supervisor exited instead of staying alive after recovery"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_propagates_cancel_on_shutdown():
    async def factory():
        await asyncio.sleep(10)

    task = asyncio.ensure_future(_snapshotter_supervise_loop(factory, backoff_s=0.01))
    await asyncio.sleep(0.02)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_stops_on_clean_return_without_hot_looping():
    calls: list[int] = []

    async def factory():
        calls.append(1)  # returns normally — run_forever should never do this

    await asyncio.wait_for(_snapshotter_supervise_loop(factory, backoff_s=0.01), timeout=1.0)

    assert len(calls) == 1, "supervisor hot-looped on a clean return instead of stopping"


@pytest.mark.asyncio
async def test_cancel_during_backoff_sleep_propagates():
    """Cancel while the supervisor is sleeping between restarts must propagate."""

    async def factory():
        raise RuntimeError("always crash")

    task = asyncio.ensure_future(_snapshotter_supervise_loop(factory, backoff_s=5.0))
    await asyncio.sleep(0.05)  # let it crash once and enter the backoff sleep
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
