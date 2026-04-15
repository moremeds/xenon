"""GET /uw-stats — UW API usage statistics endpoint.

Exposes request counts, latency percentiles, cache hit rates, and
per-ticker/per-endpoint breakdowns collected by the process-wide
``uw_api_stats.stats`` singleton.

Also surfaces the rolling 96-hour hourly history used by the
``UwApiUsageChart`` component on the /uw-analyze page.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

router = APIRouter()


@router.get("/uw-stats")
async def uw_stats() -> dict:
    """Return current UW API usage statistics with daily breakdown."""
    from utils.uw_api_stats import stats

    return stats.get_stats_with_daily()


@router.post("/uw-stats/reset")
async def uw_stats_reset() -> dict:
    """Reset session counters. Hourly history buckets are preserved —
    use POST /uw-stats/history/clear for the destructive wipe."""
    from utils.uw_api_stats import stats

    stats.reset()
    return {"status": "reset"}


@router.get("/uw-stats/history")
async def uw_stats_history(
    hours: int = Query(96, ge=1, le=168, description="Number of hourly buckets to return"),
) -> dict:
    """Return the rolling hourly history, zero-filled over the window."""
    from utils.uw_api_stats import stats

    return {"buckets": stats.get_hourly_history(hours=hours)}


@router.post("/uw-stats/history/clear")
async def uw_stats_history_clear() -> dict:
    """Destructive: wipe all hourly history buckets.

    Separate from /uw-stats/reset because hourly history persists across
    restarts and resetting it would defeat the whole point. Only call
    this for explicit operator-driven clears.
    """
    from utils.uw_api_stats import stats

    stats.clear_history()
    return {"status": "cleared"}
