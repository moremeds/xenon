"""PR-06: every `xenon-ib-sync --sync` spawn in the API process is single-flight.

Background and foreground (`/portfolio/sync`) triggers share one slot: a
background trigger while a sync runs returns "in_progress"; a foreground
request while a sync runs waits for that sync and returns its result. No
second subprocess starts in either case.

Not covered here (PR-11 prerequisite): syncs started outside the API process
(`reports/portfolio_report.py`, the CLI, cron) do not share this slot.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from xenon.api import server as server_mod
from xenon.api.subprocess import ScriptResult
from xenon.api.routes import portfolio as portfolio_routes

pytestmark = pytest.mark.asyncio

SCOPE = SimpleNamespace(broker="IB", account_env="paper", broker_account="DU0000000")


@pytest.fixture
def runner(monkeypatch):
    """Stand-in for `_run_ib_script_with_recovery` that blocks until released."""
    state = {"calls": [], "release": asyncio.Event(), "result": ScriptResult(ok=True, data={})}

    async def _fake(entry, args, timeout=30):
        state["calls"].append((entry, tuple(args)))
        await state["release"].wait()
        return state["result"]

    async def _payload(scope):
        return {"positions": [], "last_sync": None}

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", _fake)
    monkeypatch.setattr(portfolio_routes, "_read_portfolio_payload", _payload)
    monkeypatch.setattr(portfolio_routes, "_sync_task", None)
    return state


async def test_second_background_sync_while_running_is_in_progress(runner):
    first = await portfolio_routes.portfolio_background_sync()
    await asyncio.sleep(0)  # let the sync task start
    second = await portfolio_routes.portfolio_background_sync()

    assert first == {"status": "accepted"}
    assert second == {"status": "in_progress"}
    assert [c[0] for c in runner["calls"]] == ["xenon-ib-sync"]

    runner["release"].set()
    await portfolio_routes._sync_task
    third = await portfolio_routes.portfolio_background_sync()
    assert third == {"status": "accepted"}
    await portfolio_routes._sync_task
    assert len(runner["calls"]) == 2


async def test_foreground_sync_during_background_sync_joins_it(runner):
    """Fable P2-1: a foreground sync made while a background sync runs waits
    for it and returns its result; it never starts a second pass."""
    assert await portfolio_routes.portfolio_background_sync() == {"status": "accepted"}
    await asyncio.sleep(0)
    fg = asyncio.create_task(portfolio_routes.portfolio_sync(scope=SCOPE))
    await asyncio.sleep(0)
    assert not fg.done()  # waiting on the running sync
    assert len(runner["calls"]) == 1

    runner["release"].set()
    assert await fg == {"positions": [], "last_sync": None}
    assert len(runner["calls"]) == 1


async def test_background_sync_during_foreground_sync_is_in_progress(runner):
    fg = asyncio.create_task(portfolio_routes.portfolio_sync(scope=SCOPE))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert await portfolio_routes.portfolio_background_sync() == {"status": "in_progress"}
    runner["release"].set()
    await fg
    assert len(runner["calls"]) == 1


async def test_foreground_join_reports_the_shared_failure(runner):
    runner["result"] = ScriptResult(ok=False, error="gateway down")
    await portfolio_routes.portfolio_background_sync()
    await asyncio.sleep(0)
    fg = asyncio.create_task(portfolio_routes.portfolio_sync(scope=SCOPE))
    await asyncio.sleep(0)
    runner["release"].set()
    with pytest.raises(HTTPException) as exc:
        await fg
    assert exc.value.status_code == 502 and exc.value.detail == "gateway down"
    assert len(runner["calls"]) == 1


async def test_cancelled_foreground_request_does_not_cancel_the_shared_sync(runner):
    fg = asyncio.create_task(portfolio_routes.portfolio_sync(scope=SCOPE))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    fg.cancel()
    with pytest.raises(asyncio.CancelledError):
        await fg
    assert not portfolio_routes._sync_task.done()
    runner["release"].set()
    result = await portfolio_routes._sync_task
    assert result.ok


async def test_failed_background_sync_frees_the_slot(monkeypatch):
    async def _boom(entry, args, timeout=30):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(server_mod, "_run_ib_script_with_recovery", _boom)
    monkeypatch.setattr(portfolio_routes, "_sync_task", None)
    assert await portfolio_routes.portfolio_background_sync() == {"status": "accepted"}
    result = await portfolio_routes._sync_task  # logged, never raised out of the task
    assert result.ok is False
    assert await portfolio_routes.portfolio_background_sync() == {"status": "accepted"}
    await portfolio_routes._sync_task
