"""Tests for /uw-analyze/portfolio + /uw-analyze/refresh routes.

Stubs out the analyser runner so we don't need UW credentials. Uses
FastAPI TestClient against a freshly-built minimal app that registers
the route module's router.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.routes import uw_analyze as routes_mod  # noqa: E402
from api.services import uw_analyze_candidates as cand  # noqa: E402
from api.services.uw_analyze_cache import UwAnalyzeCache  # noqa: E402
from api.services.uw_analyze_flow_tracker import FlowLog  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _build_app(tmp_path, fake_runner, *, portfolio=None, watchlist=None):
    """Wire a fresh module-state cache + flow log + candidate fixtures
    into the route singletons, then mount the router on a fresh app."""
    routes_mod.reset_state_for_tests()
    cand.clear_adhoc()

    # Patch candidates source paths to fixtures.
    if portfolio is not None:
        path = tmp_path / "portfolio.json"
        path.write_text(__import__("json").dumps(portfolio))
        cand.PORTFOLIO_PATH = path
    else:
        cand.PORTFOLIO_PATH = tmp_path / "missing-portfolio.json"
    if watchlist is not None:
        path = tmp_path / "watchlist.json"
        path.write_text(__import__("json").dumps(watchlist))
        cand.WATCHLIST_PATH = path
    else:
        cand.WATCHLIST_PATH = tmp_path / "missing-watchlist.json"

    # Inject our cache + flow log singletons.
    routes_mod._portfolio_cache = UwAnalyzeCache(
        cache_path=tmp_path / "cache.json",
        market_open_fn=lambda: True,
    )
    routes_mod._flow_log = FlowLog(path=tmp_path / "flow.json")

    # Patch the runner used by the routes.
    routes_mod._runner = fake_runner  # type: ignore[attr-defined]

    app = FastAPI()
    app.include_router(routes_mod.router)
    return app


def _fake_runner(report_overrides=None, display_overrides=None, flow_alerts=None):
    async def _r(ticker: str):
        report = {
            "ticker": ticker,
            "price": 100.0,
            "regime": {"gex_sign": "POSITIVE"},
            "scores": {"flow": 5},
            "fetched_at": "2026-04-08T10:00:00",
            "data_freshness": {},
            "benchmark": {"spy": {}, "sector_etf": None},
            "vrp": {},
        }
        if report_overrides:
            report.update(report_overrides)
        display = {
            "iv_rank": 40,
            "max_pain": 100,
            "net_call_premium": 0,
            "net_put_premium": 0,
            "gex_flip": 99,
            "call_wall_strike": 110,
            "put_wall_strike": 90,
            "gex_by_strike": None,
            "sector": None,
            "iv": None,
            "rv": None,
            "gamma_per_1pct": None,
            "short_volume_ratio": None,
            "short_volume_trend": None,
            "term_structure_label": None,
        }
        if display_overrides:
            display.update(display_overrides)
        return report, display, list(flow_alerts or [])

    return _r


# ── Routes registered ──────────────────────────────────────────────────────


def test_routes_registered():
    paths = {r.path for r in routes_mod.router.routes}
    assert "/uw-analyze" in paths
    assert "/uw-analyze/portfolio" in paths
    assert "/uw-analyze/refresh" in paths


# ── /portfolio ─────────────────────────────────────────────────────────────


def test_portfolio_returns_seeded_tickers(tmp_path):
    app = _build_app(
        tmp_path,
        _fake_runner(),
        portfolio={"positions": [{"ticker": "NVDA"}]},
        watchlist={"tickers": [{"ticker": "AAPL"}]},
    )
    client = TestClient(app)
    r = client.get("/uw-analyze/portfolio")
    assert r.status_code == 200
    body = r.json()
    tickers = {row["ticker"] for row in body["tickers"]}
    assert tickers == {"NVDA", "AAPL"}
    assert body["market_state"] in ("open", "closed")
    # ttl_seconds reflects the cache singleton (market_open_fn → True),
    # so always 300 in tests.
    assert body["ttl_seconds"] == 300


def test_portfolio_merges_sources_for_overlap(tmp_path):
    app = _build_app(
        tmp_path,
        _fake_runner(),
        portfolio={"positions": [{"ticker": "NVDA"}]},
        watchlist={"tickers": [{"ticker": "NVDA"}]},
    )
    client = TestClient(app)
    body = client.get("/uw-analyze/portfolio").json()
    nvda = next(r for r in body["tickers"] if r["ticker"] == "NVDA")
    assert sorted(nvda["sources"]) == ["portfolio", "watchlist"]


def test_portfolio_returns_changes_after_force_refresh(tmp_path):
    """Two snapshots in sequence with different data should yield changes."""
    state = {"call_pnl": 0}

    async def runner(t):
        report = {
            "ticker": t,
            "price": 100,
            "regime": {"gex_sign": "POSITIVE"},
            "scores": {"flow": 5},
        }
        display = {
            "iv_rank": 40,
            "max_pain": 100,
            "net_call_premium": state["call_pnl"],
            "net_put_premium": 0,
            "gex_flip": 99,
            "call_wall_strike": 110,
            "put_wall_strike": 90,
        }
        flow_alerts = (
            [
                {
                    "option_type": "call",
                    "strike": 105,
                    "expiration_date": "2026-05-15",
                    "total_premium": 7_000_000,
                    "open_interest": 1500,
                    "volume": 4000,
                    "mid": 2.50,
                }
            ]
            if state["call_pnl"]
            else []
        )
        return report, display, flow_alerts

    app = _build_app(
        tmp_path,
        runner,
        portfolio={"positions": [{"ticker": "NVDA"}]},
    )
    client = TestClient(app)
    client.get("/uw-analyze/portfolio")
    state["call_pnl"] = 10_000_000  # +$10M call sweep
    client.post("/uw-analyze/refresh", json={"tickers": ["NVDA"]})
    body = client.get("/uw-analyze/portfolio").json()
    nvda = next(r for r in body["tickers"] if r["ticker"] == "NVDA")
    codes = {c["code"] for c in nvda["changes"]}
    assert "UNUSUAL_CALL_SWEEP" in codes
    assert any(a["code"] == "UNUSUAL_CALL_SWEEP" for a in body["action_items"])


def test_portfolio_surfaces_on_demand_oi_changes(monkeypatch, tmp_path):
    """When oi_baseline is missing, /portfolio fetches OI on-demand and
    surfaces the changes in both row.oi_changes and action_items."""
    from api.services import uw_analyze_oi_tracker
    from api.services.uw_analyze_oi_tracker import OiChange

    async def fake_fetch(client, ticker, spot):
        return [
            OiChange(
                strike=100.0,
                side="call",
                prev_oi=500,
                curr_oi=2000,
                delta=1500,
                delta_pct=3.0,
                label="+1.5K calls @ $100 (+300%)",
            )
        ]

    monkeypatch.setattr(uw_analyze_oi_tracker, "fetch_and_diff", fake_fetch)

    import clients.uw_client as uw_client_mod

    monkeypatch.setattr(uw_client_mod, "UWClient", lambda *a, **k: object())

    app = _build_app(
        tmp_path,
        _fake_runner(),
        portfolio={"positions": [{"ticker": "AAPL"}]},
    )
    client = TestClient(app)
    body = client.get("/uw-analyze/portfolio").json()
    row = next(r for r in body["tickers"] if r["ticker"] == "AAPL")
    assert row["oi_changes"]
    assert row["oi_changes"][0]["label"].startswith("+1.5K")
    assert any(a["code"] == "OI_DELTA" for a in body["action_items"])


# ── /refresh ────────────────────────────────────────────────────────────────


def test_refresh_specific_tickers(tmp_path):
    calls = []

    async def runner(t):
        calls.append(t)
        return await _fake_runner()(t)

    app = _build_app(
        tmp_path,
        runner,
        portfolio={"positions": [{"ticker": "NVDA"}, {"ticker": "AAPL"}]},
    )
    client = TestClient(app)
    r = client.post("/uw-analyze/refresh", json={"tickers": ["nvda"]})
    assert r.status_code == 200
    assert r.json()["refreshed"] == 1
    assert calls == ["NVDA"]


def test_refresh_all_when_body_empty(tmp_path):
    calls = []

    async def runner(t):
        calls.append(t)
        return await _fake_runner()(t)

    app = _build_app(
        tmp_path,
        runner,
        portfolio={"positions": [{"ticker": "NVDA"}, {"ticker": "AAPL"}]},
    )
    client = TestClient(app)
    r = client.post("/uw-analyze/refresh", json={})
    assert r.status_code == 200
    assert r.json()["refreshed"] == 2
    assert sorted(calls) == ["AAPL", "NVDA"]


def test_refresh_adhoc_adds_to_candidate_set(tmp_path):
    async def runner(t):
        return await _fake_runner()(t)

    app = _build_app(tmp_path, runner)  # no portfolio / watchlist
    client = TestClient(app)
    r = client.post("/uw-analyze/refresh", json={"tickers": ["TSLA"], "adhoc": True})
    assert r.status_code == 200
    assert r.json()["refreshed"] == 1
    body = client.get("/uw-analyze/portfolio").json()
    assert any(row["ticker"] == "TSLA" and "adhoc" in row["sources"] for row in body["tickers"])


# ── Legacy /uw-analyze still works ─────────────────────────────────────────


def test_legacy_post_route_unchanged(tmp_path):
    """The single-ticker route uses run_analysis_with_data directly; we just
    confirm the endpoint is reachable + validates ticker shape."""
    app = _build_app(tmp_path, _fake_runner())
    client = TestClient(app)
    r = client.post("/uw-analyze", json={"ticker": ""})
    assert r.status_code == 400
