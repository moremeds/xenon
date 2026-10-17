"""PR-06: /portfolio/background-sync is single-flight.

A second trigger while a background sync (and its post-sync audit) is still
running returns "in_progress" and spawns no second subprocess.
"""

from __future__ import annotations

import asyncio

import pytest

from xenon.api import server as server_mod
from xenon.api.subprocess import ScriptResult

pytestmark = pytest.mark.asyncio


@pytest.fixture
def runner(monkeypatch):
    """Stand-in for `_run_ib_script_with_recovery` that blocks until released."""
    state = {"calls": [], "release": asyncio.Event()}

    async def _fake(entry, args, timeout=30):
        state["calls"].append((entry, tuple(args)))
        await state["release"].wait()
        return ScriptResult(ok=True, data={})

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", _fake)
    monkeypatch.setattr(server_mod, "_bg_sync_task", None, raising=False)
    return state


async def test_second_background_sync_while_running_is_in_progress(runner):
    first = await server_mod.portfolio_background_sync()
    await asyncio.sleep(0)  # let the sync task start
    second = await server_mod.portfolio_background_sync()

    assert first == {"status": "accepted"}
    assert second == {"status": "in_progress"}
    assert [c[0] for c in runner["calls"]] == ["xenon-ib-sync"]

    runner["release"].set()
    await server_mod._bg_sync_task
    third = await server_mod.portfolio_background_sync()
    assert third == {"status": "accepted"}
    await server_mod._bg_sync_task
    assert len(runner["calls"]) == 2


async def test_failed_background_sync_frees_the_slot(monkeypatch):
    async def _boom(entry, args, timeout=30):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", _boom)
    monkeypatch.setattr(server_mod, "_bg_sync_task", None, raising=False)
    assert await server_mod.portfolio_background_sync() == {"status": "accepted"}
    await server_mod._bg_sync_task  # logged, never raised out of the task
    assert await server_mod.portfolio_background_sync() == {"status": "accepted"}
    await server_mod._bg_sync_task
