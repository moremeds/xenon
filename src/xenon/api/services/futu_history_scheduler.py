"""M9 — Futu history scheduler.

Two surfaces:

  - `next_run_at_et(now_et, hour=16, minute=30)`: pure scheduling logic
    that the loop and tests both call. Returns the next future weekday
    occurrence of HH:MM Eastern Time, strictly after `now_et`. Skips
    Sat/Sun.

  - `futu_history_loop(engine_factory, scope_factory, ...)`: forever
    asyncio loop. Sleeps until the next 16:30 ET weekday, runs the sync
    end-to-end per scope (M3 → M4 → M5 via run_history_sync), logs, loops. Cancelled
    cleanly when the lifespan shutdown task awaits the cancellation.

US-market-holiday calendar is intentionally NOT modeled here — Futu's
history endpoints return cleanly on no-activity days (empty deal list,
empty cashflow for that clearing_date). A holiday-run is wasted but not
incorrect. Add a calendar later if it materially shifts cost.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from xenon.db.service_health import record_service_health

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# scopes_factory() may open the Futu SDK context, which retries a refused OpenD
# forever. Run it off the event loop with a bound so a dead OpenD costs one
# failed tick, not a frozen API (2026-10 nightly wedge).
SCOPE_TIMEOUT_S = 30.0


def next_run_at_et(now_et: datetime, *, hour: int = 16, minute: int = 30) -> datetime:
    """Return the next strict-future ET weekday occurrence of HH:MM.

    Strict-future: if `now_et` is exactly at HH:MM, the next occurrence is
    the FOLLOWING weekday's HH:MM. This prevents a tick that just fired
    from being scheduled to fire immediately again.
    """
    if now_et.tzinfo is None:
        raise ValueError("now_et must be timezone-aware")

    target = now_et.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_et:
        target = target + timedelta(days=1)
    while target.weekday() >= 5:  # 5=Sat, 6=Sun
        target = target + timedelta(days=1)
    return target


async def futu_history_loop(
    engine_factory: Callable[[], Any],
    scopes_factory: Callable[[], Any],
    *,
    runner: Callable[..., Any],
    hour: int = 16,
    minute: int = 30,
) -> None:
    """Forever loop: sleep until next 16:30 ET weekday, run sync, loop.

    Parameters
    ----------
    engine_factory : zero-arg, returns an AsyncEngine bound to the current
        DATABASE_URL. We don't accept a long-lived engine because dev-mode
        reloads may invalidate it.
    scopes_factory : zero-arg, returns the FUTU AccountScopes to sync, in
        order (live only: Futu serves history to live accounts). Allowed to connect to OpenD to
        resolve a legacy scope (spec §10); runs off the event loop.
    runner : usually `xenon.cli.futu_history_sync.run_history_sync`. Tests
        pass a synchronous spy.

    Cancellation: standard asyncio. The lifespan stores the task handle
    and `await task` after cancel() — this loop just propagates
    CancelledError out of asyncio.sleep().
    """
    while True:
        now_et = datetime.now(tz=ET)
        next_at = next_run_at_et(now_et, hour=hour, minute=minute)
        sleep_s = (next_at - now_et).total_seconds()
        logger.info(
            "futu history loop: sleeping %.0fs until %s",
            sleep_s,
            next_at.isoformat(),
        )
        await asyncio.sleep(sleep_s)

        try:
            scopes = await asyncio.wait_for(
                asyncio.to_thread(scopes_factory), timeout=SCOPE_TIMEOUT_S
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # No scope resolved: record under broker="FUTU" so the operator
            # console still shows the failed night.
            logger.exception("futu history scope resolution failed; will retry next weekday")
            record_service_health(
                "futu_history",
                "error",
                error={"msg": str(exc)},
                broker="FUTU",
                finished_at=datetime.now(tz=ET),
            )
            continue
        # Sequential, one OpenD context at a time; one scope failing must not
        # skip the next one or poison the schedule.
        for scope in scopes:
            await _run_scope(engine_factory, scope, runner)


async def _run_scope(engine_factory: Callable[[], Any], scope: Any, runner: Callable[..., Any]) -> None:
    """One scope's nightly sync; the heartbeat lands in that scope's partition.

    The Futu SDK calls are blocking (connect, fetches, per-day throttle
    sleeps — an inception backfill runs for minutes), so the whole sync runs
    on a worker thread with its own event loop and engine, never on the API's.
    """
    try:
        result = await asyncio.to_thread(asyncio.run, _sync_one(engine_factory, scope, runner))
        logger.info(
            "futu history sync ok (%s): trades=%s cashflows=%s nav_rows=%s",
            scope.account_env,
            result.get("trades_inserted"),
            result.get("cashflows_inserted"),
            result.get("nav_rows_written"),
        )
        record_service_health(
            "futu_history",
            "ok",
            broker=scope.broker,
            account_env=scope.account_env,
            broker_account=scope.broker_account,
            finished_at=datetime.now(tz=ET),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("futu history sync failed (%s); will retry next weekday", scope.account_env)
        record_service_health(
            "futu_history",
            "error",
            error={"msg": str(exc)},
            broker=scope.broker,
            account_env=scope.account_env,
            broker_account=scope.broker_account,
            finished_at=datetime.now(tz=ET),
        )


async def _sync_one(engine_factory: Callable[[], Any], scope: Any, runner: Callable[..., Any]) -> dict:
    """Runs inside the worker thread's loop: the engine must live and die there."""
    engine = engine_factory()
    try:
        return await runner(engine, scope, since=None)
    finally:
        try:
            await engine.dispose()
        except Exception:  # noqa: BLE001
            pass


__all__ = ("next_run_at_et", "futu_history_loop")
