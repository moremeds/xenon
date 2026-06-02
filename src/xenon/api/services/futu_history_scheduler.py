"""M9 — Futu history scheduler.

Two surfaces:

  - `next_run_at_et(now_et, hour=16, minute=30)`: pure scheduling logic
    that the loop and tests both call. Returns the next future weekday
    occurrence of HH:MM Eastern Time, strictly after `now_et`. Skips
    Sat/Sun.

  - `futu_history_loop(engine_factory, scope_factory, ...)`: forever
    asyncio loop. Sleeps until the next 16:30 ET weekday, runs the sync
    end-to-end (M3 → M4 → M5 via run_history_sync), logs, loops. Cancelled
    cleanly when the lifespan shutdown task awaits the cancellation.

US-market-holiday calendar is intentionally NOT modeled here — Futu's
history endpoints return cleanly on no-activity days (empty deal list,
empty cashflow for that clearing_date). A holiday-run is wasted but not
incorrect. Add a calendar later if it materially shifts cost.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")


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
    scope_factory: Callable[[], Any],
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
    scope_factory : zero-arg, returns the FUTU AccountScope. Allowed to
        connect to OpenD to resolve scope (spec §10).
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
            engine = engine_factory()
            scope = scope_factory()
            result = await runner(engine, scope, since=None)
            logger.info(
                "futu history sync ok: trades=%s cashflows=%s nav_rows=%s",
                result.get("trades_inserted"),
                result.get("cashflows_inserted"),
                result.get("nav_rows_written"),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # One failure must not poison the schedule. Log and loop back —
            # the next 16:30 ET will try again.
            logger.exception("futu history sync failed; will retry next weekday")
        finally:
            try:
                await engine.dispose()
            except Exception:  # noqa: BLE001
                pass


__all__ = ("next_run_at_et", "futu_history_loop")
