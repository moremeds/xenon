"""UW Analyze daily job — fires at 15:50 ET on trading days.

Runs once per trading day to:
- Snapshot OI deltas via the OI tracker (fed back into cache entries
  as `oi_baseline`).
- Advance the daily_track for every open FlowEvent in the unusual
  flow log; classify anomalies; close out unwound positions.

Lifecycle: started from FastAPI lifespan (`scripts/api/server.py`),
runs as a long-lived asyncio task that loops:

    while True:
        delay = seconds_until_next_trigger(now_et)
        await asyncio.sleep(delay)
        if not is_trading_day(now_et):
            continue
        await run_once(...)

A module-level `_job_running` flag guards against accidental double-start
across hot reloads.

Spec: docs/superpowers/specs/2026-04-08-uw-analyze-overhaul-design.md
      §"Daily OI tracker" / §"Unusual flow lifecycle tracker"
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Awaitable, Callable, Optional

try:
    import pytz  # type: ignore

    _ET = pytz.timezone("America/New_York")
except Exception:  # noqa: BLE001
    from datetime import timezone  # type: ignore

    _ET = timezone(timedelta(hours=-5))  # crude fallback

_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

logger = logging.getLogger("xenon.uw_analyze_daily_job")

# 15:50 ET — 10 minutes before close so EOD OI is ready and we still have time
# to advance daily tracks before the next trading day starts.
DAILY_TRIGGER = time(15, 50)

_job_running = False


def now_et() -> datetime:
    return datetime.now(_ET)


def now_et_date() -> date:
    return now_et().date()


def trading_days_between(d1: date, d2: date) -> int:
    """Count of trading days strictly after d1 up to and including d2.

    Returns 0 if d2 <= d1. Respects weekends + is_trading_day() holiday calendar.
    """
    if d2 <= d1:
        return 0
    n = 0
    cur = d1
    while cur < d2:
        cur = cur + timedelta(days=1)
        dt = datetime.combine(cur, time(12, 0))
        if hasattr(_ET, "localize"):
            dt = _ET.localize(dt)
        else:
            dt = dt.replace(tzinfo=_ET)
        if is_trading_day(dt):
            n += 1
    return n


def is_trading_day(dt: datetime) -> bool:
    """Best-effort: weekday + market_calendar holiday check if available."""
    if dt.weekday() >= 5:
        return False
    try:
        from utils.market_calendar import load_holidays

        holidays = load_holidays(dt.year)
        if dt.strftime("%Y-%m-%d") in holidays:
            return False
    except Exception:  # noqa: BLE001
        pass
    return True


def seconds_until_next_trigger(now: Optional[datetime] = None) -> float:
    """Compute wall-clock seconds from `now` until the next 15:50 ET trigger
    on a trading day."""
    now = now or now_et()
    if now.tzinfo is None:
        now = _ET.localize(now) if hasattr(_ET, "localize") else now.replace(tzinfo=_ET)
    target_today = (
        _ET.localize(datetime.combine(now.date(), DAILY_TRIGGER))
        if hasattr(_ET, "localize")
        else datetime.combine(now.date(), DAILY_TRIGGER).replace(tzinfo=_ET)
    )

    candidate = target_today
    if candidate <= now:
        # Move to next day.
        candidate = candidate + timedelta(days=1)
    # Skip non-trading days
    while not is_trading_day(candidate):
        candidate = candidate + timedelta(days=1)
        candidate = candidate.replace(hour=DAILY_TRIGGER.hour, minute=DAILY_TRIGGER.minute, second=0, microsecond=0)
    return max(0.0, (candidate - now).total_seconds())


# ── Run-once orchestration ─────────────────────────────────────────────────


async def run_once(
    *,
    cache,
    flow_log,
    uw_client,
    oi_fetcher: Optional[Callable[[str, Optional[float]], Awaitable[list]]] = None,
    contract_fetcher: Optional[Callable[[str], Awaitable[Optional[dict]]]] = None,
) -> dict:
    """Single end-to-end pass.

    - For every cache entry, refresh OI via the OI tracker and stash on
      `entry["oi_baseline"]` for the next /portfolio call to surface.
    - For every open FlowEvent, advance daily_track using contract data.

    `oi_fetcher` and `contract_fetcher` are injectable so tests can drive the
    job without UW.
    """
    from api.services.uw_analyze_flow_tracker import progress_event
    from api.services.uw_analyze_oi_tracker import fetch_and_diff

    if oi_fetcher is None:

        async def oi_fetcher(ticker: str, spot):  # type: ignore[no-redef]
            return await fetch_and_diff(uw_client, ticker, spot)

    today_iso = now_et_date().isoformat()
    stats = {"tickers_oi": 0, "events_advanced": 0, "events_anomaly": 0, "events_closed": 0}

    # ── OI deltas ─────────────────────────────────────────────────────
    for ticker, entry in cache.all_entries().items():
        snap = entry.get("current") if isinstance(entry, dict) else None
        spot = None
        if isinstance(snap, dict):
            derived = snap.get("derived") or {}
            spot = derived.get("spot") or (snap.get("report") or {}).get("price")
        try:
            changes = await oi_fetcher(ticker, spot)
        except Exception as exc:  # noqa: BLE001
            logger.warning("oi fetch failed for %s: %s", ticker, exc)
            continue
        # Stash on cache entry
        entry["oi_baseline"] = {
            "data_date": today_iso,
            "changes": [c.to_dict() if hasattr(c, "to_dict") else c for c in changes],
        }
        stats["tickers_oi"] += 1
    await cache._persist()

    # ── Flow event progression ─────────────────────────────────────────
    flow_log.load()
    for event in flow_log.all():
        if event.status != "open":
            continue
        contract_state = None
        if contract_fetcher is not None:
            try:
                contract_state = await contract_fetcher(event.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("contract fetch failed for %s: %s", event.id, exc)
        if not contract_state:
            # Without fresh data we can only check for expiry-based closeout.
            from api.services.uw_analyze_flow_tracker import maybe_close_or_expire

            maybe_close_or_expire(event)
            flow_log.replace(event)
            continue
        progress_event(
            event,
            today=today_iso,
            oi=int(contract_state.get("oi", event.initial.oi)),
            mid=float(contract_state.get("mid", event.initial.mid)),
            underlying_price=float(contract_state.get("underlying_price", event.initial.underlying_price)),
            volume=int(contract_state.get("volume", 0)),
        )
        stats["events_advanced"] += 1
        if event.status == "anomaly":
            stats["events_anomaly"] += 1
        elif event.status == "closed":
            stats["events_closed"] += 1
        flow_log.replace(event)
    flow_log.save()

    logger.info("uw_analyze_daily_job run_once stats: %s", stats)
    return stats


# ── Long-lived loop ────────────────────────────────────────────────────────


async def run_loop(
    *,
    cache,
    flow_log,
    uw_client,
    oi_fetcher=None,
    contract_fetcher=None,
    test_trigger_now: bool = False,
):
    """Long-lived loop suitable for `asyncio.create_task` from lifespan.

    Set env var `UW_ANALYZE_JOB_TEST_TRIGGER=now` to fire once immediately
    on startup (skipping the wait), useful for smoke testing.
    """
    global _job_running
    if _job_running:
        logger.warning("uw_analyze_daily_job already running — skipping start")
        return
    _job_running = True
    logger.info("uw_analyze_daily_job loop started")

    try:
        if test_trigger_now or os.environ.get("UW_ANALYZE_JOB_TEST_TRIGGER") == "now":
            try:
                await run_once(
                    cache=cache,
                    flow_log=flow_log,
                    uw_client=uw_client,
                    oi_fetcher=oi_fetcher,
                    contract_fetcher=contract_fetcher,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("uw_analyze_daily_job test trigger failed: %s", exc)

        while True:
            delay = seconds_until_next_trigger()
            logger.info("uw_analyze_daily_job sleeping %.0fs until next 15:50 ET", delay)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                logger.info("uw_analyze_daily_job cancelled — exiting loop")
                raise
            if not is_trading_day(now_et()):
                continue
            try:
                await run_once(
                    cache=cache,
                    flow_log=flow_log,
                    uw_client=uw_client,
                    oi_fetcher=oi_fetcher,
                    contract_fetcher=contract_fetcher,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("uw_analyze_daily_job run_once failed: %s", exc)
    finally:
        _job_running = False
