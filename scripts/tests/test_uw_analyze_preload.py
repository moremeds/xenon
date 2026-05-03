"""Lifespan preload test for uw_analyze_cache.

Verifies that FastAPI startup eagerly loads the cache from PG into memory
before the first request path touches it. Without this, the first GET
after a restart pays the round-trip cost.

Post-PG-cutoff: the cache is hydrated from xenon.uw_analyze_snapshots
(latest row per ticker), not a JSON file. We seed a single row in the
test DB and assert the lifespan preload populates the singleton.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Do NOT enable XENON_API_TEST_MODE here — this test needs the real
# lifespan() non-test branch to execute the preload. We monkeypatch the
# heavy IB/UW startup steps below.


def test_lifespan_preloads_uw_analyze_cache(monkeypatch, pg_test_engine):
    """Seed PG row, enter lifespan(), assert singleton is loaded."""
    from sqlalchemy import delete, insert

    from xenon.api.routes import uw_analyze as route_mod
    from xenon.db.schema import uw_analyze_snapshots

    # Seed one snapshot row so the preload has something to surface.
    with pg_test_engine.begin() as conn:
        conn.execute(delete(uw_analyze_snapshots).where(uw_analyze_snapshots.c.ticker == "NVDAPRELOAD"))
        conn.execute(
            insert(uw_analyze_snapshots).values(
                ticker="NVDAPRELOAD",
                report={"price": 100.0, "scores": {"composite": 50}},
                display={"iv": 0.30},
                derived={},
                dark_pool_summary=None,
                options_flow_summary=None,
                flow_alerts=[],
                materialized_changes=[],
                sources=["portfolio"],
                oi_baseline=None,
                previous_snapshot=None,
                report_fetched_at=datetime(2026, 4, 8, tzinfo=timezone.utc),
                archived_at=datetime(2026, 4, 8, tzinfo=timezone.utc),
            )
        )

    route_mod.reset_state_for_tests()

    # api.server pulls in eventkit which needs an event loop on the thread
    # at import time. Set one before importing.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    from fastapi import FastAPI

    from xenon.api import server as server_mod

    # Force non-test-mode so the lifespan preload branch runs. Stub out
    # the heavy IB/UW steps so we don't need a real gateway.
    monkeypatch.setenv("XENON_API_TEST_MODE", "0")

    async def _fake_ensure_gw():
        return {"ok": True}

    monkeypatch.setattr(server_mod, "ensure_ib_gateway", _fake_ensure_gw)

    class _FakePool:
        async def connect_all(self):
            return {"ok": True}

        async def close_all(self):
            pass

    monkeypatch.setattr(server_mod, "IBPool", _FakePool)
    # Drop the daily job worker by claiming a non-zero worker id.
    monkeypatch.setenv("XENON_DAILY_JOB_WORKER_ID", "1")

    app = FastAPI()

    async def go():
        async with server_mod.lifespan(app):
            singleton = route_mod.get_portfolio_cache()
            assert singleton._loaded is True, "preload did not run"
            entries = singleton.all_entries()
            assert "NVDAPRELOAD" in entries, f"seeded entry missing: {list(entries)}"

    try:
        loop.run_until_complete(go())
    finally:
        # Reset for other tests + cleanup PG.
        route_mod.reset_state_for_tests()
        with pg_test_engine.begin() as conn:
            conn.execute(delete(uw_analyze_snapshots).where(uw_analyze_snapshots.c.ticker == "NVDAPRELOAD"))
