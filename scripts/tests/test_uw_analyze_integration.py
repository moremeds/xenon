"""Full-stack integration test for /uw-analyze pipeline.

Exercises real route code end-to-end: boots FastAPI, seeds two successive
snapshots with a large net_call_premium delta, and asserts the
UNUSUAL_CALL_SWEEP Change + flow event + action item surface through
/uw-analyze/portfolio.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xenon.api.routes import uw_analyze as routes_mod  # noqa: E402
from xenon.api.services import uw_analyze_candidates as cand  # noqa: E402
from xenon.api.services.uw_analyze_cache import UwAnalyzeCache  # noqa: E402
from xenon.api.services.uw_analyze_flow_tracker import FlowLog  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def test_full_stack_refresh_then_portfolio_surfaces_sweep_event(monkeypatch, tmp_path):
    # Reset routes module state.
    routes_mod.reset_state_for_tests()
    cand.clear_adhoc()

    # Point candidate loader at a fixture with NVDA only.
    portfolio_path = tmp_path / "portfolio.json"
    portfolio_path.write_text(__import__("json").dumps({"positions": [{"ticker": "NVDA"}]}))
    cand.PORTFOLIO_PATH = portfolio_path
    cand.WATCHLIST_PATH = tmp_path / "missing-watchlist.json"

    # Inject fresh cache + flow log singletons scoped to tmp_path.
    routes_mod._portfolio_cache = UwAnalyzeCache(
        cache_path=tmp_path / "cache.json",
        market_open_fn=lambda: True,
    )
    routes_mod._flow_log = FlowLog(path=tmp_path / "flow.json")

    # Two-call runner: baseline, then sweep with +$10M net_call_premium delta.
    call_count = {"n": 0}

    async def fake_runner(ticker: str):
        call_count["n"] += 1
        ncp = 100_000 if call_count["n"] == 1 else 10_200_000
        report = {
            "ticker": ticker,
            "price": 100.0,
            "regime": {"gex_sign": "POSITIVE"},
            "scores": {"flow": 60},
        }
        display = {
            "iv_rank": 50.0,
            "max_pain": 99.0,
            "net_call_premium": ncp,
            "net_put_premium": -500_000,
            "gex_flip": 99,
            "call_wall_strike": 110,
            "put_wall_strike": 90,
            "gex_by_strike": {
                "strikes": [
                    {"strike": 95.0, "call_gamma": 100.0, "put_gamma": -50.0, "gamma": 50.0},
                    {"strike": 100.0, "call_gamma": 200.0, "put_gamma": -100.0, "gamma": 100.0},
                    {"strike": 105.0, "call_gamma": 300.0, "put_gamma": -150.0, "gamma": 150.0},
                    {"strike": 110.0, "call_gamma": 400.0, "put_gamma": -200.0, "gamma": 200.0},
                ]
            },
        }
        flow_alerts = [
            {
                "option_type": "call",
                "strike": 105,
                "expiration_date": "2026-09-15",
                "total_premium": 11_000_000,
                "open_interest": 800,
                "volume": 1500,
                "mid": 2.5,
            }
        ]
        return report, display, flow_alerts

    routes_mod._runner = fake_runner  # type: ignore[attr-defined]

    # Stub on-demand OI fetch + UWClient so /portfolio's missing-baseline
    # branch is harmless.
    async def fake_fetch(client, ticker, spot):
        return []

    monkeypatch.setattr("xenon.api.services.uw_analyze_oi_tracker.fetch_and_diff", fake_fetch)
    import xenon.clients.uw_client as uw_client_mod

    monkeypatch.setattr(uw_client_mod, "UWClient", lambda *a, **k: object())

    app = FastAPI()
    app.include_router(routes_mod.router)
    client = TestClient(app)

    # Seed baseline.
    r1 = client.post("/uw-analyze/refresh", json={"tickers": ["NVDA"]})
    assert r1.status_code == 200

    # Second refresh triggers the sweep diff + flow event capture.
    r2 = client.post("/uw-analyze/refresh", json={"tickers": ["NVDA"]})
    assert r2.status_code == 200

    # Read back portfolio.
    r3 = client.get("/uw-analyze/portfolio")
    assert r3.status_code == 200
    body = r3.json()
    row = next(r for r in body["tickers"] if r["ticker"] == "NVDA")

    assert any(c["code"] == "UNUSUAL_CALL_SWEEP" for c in row["changes"]), (
        f"expected UNUSUAL_CALL_SWEEP in changes, got: {row['changes']}"
    )
    assert row["unusual_flow_events"], "flow event must be captured after refresh"
    assert any(a["code"] == "UNUSUAL_CALL_SWEEP" for a in body["action_items"]), (
        f"expected UNUSUAL_CALL_SWEEP in action_items, got: {body['action_items']}"
    )
