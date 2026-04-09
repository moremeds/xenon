"""Lifespan preload test for uw_analyze_cache.

Verifies that FastAPI startup eagerly loads the cache JSON into memory
before the first request path touches it. Without this, the first GET
after a restart pays the disk-read + parse cost (~2 MB).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Do NOT enable XENON_API_TEST_MODE here — this test needs the real
# lifespan() non-test branch to execute the preload. We monkeypatch the
# heavy IB/UW startup steps below.


def test_lifespan_preloads_uw_analyze_cache(tmp_path, monkeypatch):
    """Seed cache.json, enter lifespan(), assert singleton is loaded."""
    # Point the default cache path at a seeded fixture BEFORE the module
    # singleton is constructed.
    seeded = {
        "updated_at": "2026-04-08T00:00:00+00:00",
        "entries": {
            "NVDA": {
                "current": {
                    "ticker": "NVDA",
                    "ts": "2026-04-08T00:00:00+00:00",
                    "report": {},
                    "display": {},
                    "flow_alerts": [],
                    "derived": {},
                    "dark_pool_summary": None,
                    "options_flow_summary": None,
                },
                "previous": None,
                "oi_baseline": None,
                "sources": ["portfolio"],
                "materialized_changes": [],
            }
        },
    }
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps(seeded))

    from api.routes import uw_analyze as route_mod
    from api.services import uw_analyze_cache as cache_mod

    monkeypatch.setattr(cache_mod, "_DEFAULT_CACHE_PATH", cache_file)
    route_mod.reset_state_for_tests()

    # api.server pulls in eventkit which needs an event loop on the thread
    # at import time. Set one before importing.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    from api import server as server_mod
    from fastapi import FastAPI

    # Force non-test-mode so the lifespan preload branch runs. Stub out
    # the heavy IB/UW steps so we don't need a real gateway.
    monkeypatch.setattr(server_mod, "test_mode", False)

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
            # Singleton must be constructed AND loaded by now, without
            # anyone having hit the route.
            singleton = route_mod.get_portfolio_cache()
            assert singleton._loaded is True, "preload did not run"
            entries = singleton.all_entries()
            assert "NVDA" in entries, f"seeded entry missing: {list(entries)}"

    loop.run_until_complete(go())
    # Reset for other tests.
    route_mod.reset_state_for_tests()
