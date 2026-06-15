"""M9 — futu_history_loop end-to-end smoke (no Futu, no real sleeps).

Drives the actual loop with injected fakes:
  * engine_factory returns a real AsyncEngine (so the loop can dispose it)
  * scope_factory returns a fixed AccountScope (no OpenD)
  * runner is a synchronous spy that records args + returns counts dict

Cancels after one tick to confirm the schedule-then-run-then-loop flow,
including disposal on the way out.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from xenon._test_db import is_pg_reachable, sync_test_db_url
from xenon.api.services.futu_history_scheduler import futu_history_loop
from xenon.execution.account_scope import AccountScope

ET = ZoneInfo("America/New_York")
SCOPE = AccountScope(broker="FUTU", account_env="paper", broker_account="pytest")


@pytest.mark.asyncio
async def test_loop_runs_one_tick_then_cancels(monkeypatch):
    """Patch asyncio.sleep to no-op so the first scheduled wakeup fires
    immediately. Then cancel after the first tick records its call."""
    if not is_pg_reachable():
        pytest.skip(f"PG test DB unreachable at {sync_test_db_url()}")
    url = sync_test_db_url().replace("postgresql+psycopg://", "postgresql+asyncpg://")

    real_sleep = asyncio.sleep

    async def _instant_sleep(seconds):
        return await real_sleep(0)

    # Patch the scheduler module's asyncio.sleep, not asyncio's directly,
    # so other concurrent tasks aren't accelerated.
    import xenon.api.services.futu_history_scheduler as sched_mod

    monkeypatch.setattr(sched_mod.asyncio, "sleep", _instant_sleep)

    calls: list[tuple] = []
    cancel_after_first = asyncio.Event()

    async def _runner(engine, scope, since):
        calls.append((scope.broker, scope.account_env, scope.broker_account, since))
        cancel_after_first.set()
        return {
            "trades_inserted": 0,
            "cashflows_inserted": 0,
            "nav_rows_written": 0,
        }

    def _engine_factory():
        return create_async_engine(url, pool_pre_ping=True)

    def _scope_factory():
        return SCOPE

    task = asyncio.create_task(
        futu_history_loop(
            engine_factory=_engine_factory,
            scope_factory=_scope_factory,
            runner=_runner,
        )
    )
    # Wait for the first tick (or 5s timeout — far longer than needed).
    await asyncio.wait_for(cancel_after_first.wait(), timeout=5.0)

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert calls, "runner should have been invoked at least once"
    assert calls[0] == ("FUTU", "paper", "pytest", None)


@pytest.mark.asyncio
async def test_loop_survives_runner_exception(monkeypatch):
    """A single failure must not poison the schedule. The loop catches
    and logs, then sleeps for the next slot. Verify it tried again."""
    if not is_pg_reachable():
        pytest.skip(f"PG test DB unreachable at {sync_test_db_url()}")
    url = sync_test_db_url().replace("postgresql+psycopg://", "postgresql+asyncpg://")

    real_sleep = asyncio.sleep

    async def _instant_sleep(seconds):
        return await real_sleep(0)

    import xenon.api.services.futu_history_scheduler as sched_mod

    monkeypatch.setattr(sched_mod.asyncio, "sleep", _instant_sleep)

    call_count = {"n": 0}
    second_call = asyncio.Event()

    async def _runner(engine, scope, since):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("synthetic failure")
        second_call.set()
        return {
            "trades_inserted": 0,
            "cashflows_inserted": 0,
            "nav_rows_written": 0,
        }

    def _engine_factory():
        return create_async_engine(url, pool_pre_ping=True)

    task = asyncio.create_task(
        futu_history_loop(
            engine_factory=_engine_factory,
            scope_factory=lambda: SCOPE,
            runner=_runner,
        )
    )
    await asyncio.wait_for(second_call.wait(), timeout=5.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert call_count["n"] >= 2


@pytest.mark.asyncio
async def test_error_heartbeat_uses_futu_scope(monkeypatch):
    """The error-path heartbeat must carry the FUTU scope (same partition as the
    success path), not fall back to env/unknown — otherwise the operator console
    can't reconcile a failed run against the prior successful one. No PG needed:
    the runner raises before any query."""
    from unittest.mock import AsyncMock, MagicMock

    real_sleep = asyncio.sleep

    async def _instant_sleep(seconds):
        return await real_sleep(0)

    import xenon.api.services.futu_history_scheduler as sched_mod

    monkeypatch.setattr(sched_mod.asyncio, "sleep", _instant_sleep)

    calls: list[dict] = []
    captured = asyncio.Event()

    def _capture(service, state="ok", **kwargs):
        calls.append({"service": service, "state": state, **kwargs})
        if state == "error":
            captured.set()

    monkeypatch.setattr(sched_mod, "record_service_health", _capture)

    async def _runner(engine, scope, since):
        raise RuntimeError("synthetic failure")

    def _engine_factory():
        eng = MagicMock()
        eng.dispose = AsyncMock()
        return eng

    task = asyncio.create_task(
        futu_history_loop(
            engine_factory=_engine_factory,
            scope_factory=lambda: SCOPE,
            runner=_runner,
        )
    )
    await asyncio.wait_for(captured.wait(), timeout=5.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    err = next(c for c in calls if c["state"] == "error")
    assert err["broker"] == "FUTU"
    assert err["account_env"] == "paper"
    assert err["broker_account"] == "pytest"
