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

import pytest  # noqa: E402
from api.routes import uw_analyze as routes_mod  # noqa: E402
from api.services import uw_analyze_candidates as cand  # noqa: E402
from api.services.uw_analyze_cache import UwAnalyzeCache  # noqa: E402
from api.services.uw_analyze_flow_tracker import FlowLog  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

_original_runner = routes_mod._runner
_original_portfolio_cache = routes_mod._portfolio_cache
_original_flow_log = routes_mod._flow_log
_original_portfolio_path = cand.PORTFOLIO_PATH
_original_watchlist_path = cand.WATCHLIST_PATH


@pytest.fixture(autouse=True)
def _restore_module_state():
    """Restore all mutated module attributes to prevent cross-file pollution."""
    yield
    routes_mod._runner = _original_runner
    routes_mod._portfolio_cache = _original_portfolio_cache
    routes_mod._flow_log = _original_flow_log
    cand.PORTFOLIO_PATH = _original_portfolio_path
    cand.WATCHLIST_PATH = _original_watchlist_path
    routes_mod.reset_state_for_tests()


def _build_app(tmp_path, fake_runner, *, portfolio=None, watchlist=None, market_open=True):
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
        market_open_fn=lambda: market_open,
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
    # Portfolio (NVDA) + watchlist (AAPL) must be present; static universe
    # tickers (SPY, QQQ, …) are also included by seed_candidates().
    assert {"NVDA", "AAPL"} <= tickers
    assert body["market_state"] in ("open", "closed")
    # ttl_seconds reflects the cache singleton (market_open_fn → True),
    # so it returns the OPEN TTL default (1800s / 30 min — see
    # silly-humming-tide.md plan §1 for the budget rationale).
    assert body["ttl_seconds"] == 1800


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


def test_refresh_captures_flow_events(tmp_path):
    """POST /refresh must capture flow events on the write path so a
    subsequent GET /portfolio (fresh TTL, did_refresh=False) surfaces them."""
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
    # Seed baseline via refresh (no sweep yet).
    r0 = client.post("/uw-analyze/refresh", json={"tickers": ["NVDA"]})
    assert r0.status_code == 200
    # Flip state and refresh again — this is where capture MUST run.
    state["call_pnl"] = 10_000_000
    r1 = client.post("/uw-analyze/refresh", json={"tickers": ["NVDA"]})
    assert r1.status_code == 200
    # Now GET /portfolio on fresh TTL (did_refresh=False). Events must
    # still be present because /refresh already captured them.
    body = client.get("/uw-analyze/portfolio").json()
    nvda = next(r for r in body["tickers"] if r["ticker"] == "NVDA")
    events = nvda["unusual_flow_events"]
    assert events, "expected /refresh to have captured flow events"
    assert any(
        ev.get("ticker") == "NVDA" and ev.get("side") == "call" and float(ev.get("strike", 0)) == 105.0 for ev in events
    ), f"expected FLOW event from sweep; got {events}"


def test_portfolio_does_not_re_capture_on_repeat_get(tmp_path):
    """Second GET must not create duplicate flow events — flow capture is
    a write-path operation, triggered only on a fresh refresh."""
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
    # First GET seeds baseline (no changes yet).
    client.get("/uw-analyze/portfolio")
    # Flip upstream state and expire the cache entry so the next GET
    # triggers a fresh refresh (did_refresh=True) inside the /portfolio
    # path, which is where flow capture runs.
    state["call_pnl"] = 10_000_000
    from datetime import datetime, timedelta
    from datetime import timezone as _tz

    old_ts = (datetime.now(_tz.utc) - timedelta(hours=1)).isoformat()
    routes_mod._portfolio_cache._entries["NVDA"]["current"]["ts"] = old_ts

    r1 = client.get("/uw-analyze/portfolio")
    r2 = client.get("/uw-analyze/portfolio")
    events_1 = sum(len(row["unusual_flow_events"]) for row in r1.json()["tickers"])
    events_2 = sum(len(row["unusual_flow_events"]) for row in r2.json()["tickers"])
    assert events_1 >= 1, "expected at least one captured flow event after refresh"
    assert events_2 == events_1, "second GET must not create duplicate flow events"


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

    import xenon.clients.uw_client as uw_client_mod

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


def test_portfolio_refreshes_stale_oi_baseline(monkeypatch, tmp_path):
    """When oi_baseline.data_date is from a prior trading day, /portfolio
    must refetch and persist a fresh baseline stamped with today's ET date."""
    from api.routes import uw_analyze as routes_mod
    from api.services import uw_analyze_oi_tracker
    from api.services.uw_analyze_daily_job import now_et_date
    from api.services.uw_analyze_oi_tracker import OiChange

    calls = {"n": 0}

    async def fake_fetch(client, ticker, spot):
        calls["n"] += 1
        return [
            OiChange(
                strike=200.0,
                side="put",
                prev_oi=100,
                curr_oi=900,
                delta=800,
                delta_pct=8.0,
                label="+800 puts @ $200 (+800%)",
            )
        ]

    monkeypatch.setattr(uw_analyze_oi_tracker, "fetch_and_diff", fake_fetch)
    import xenon.clients.uw_client as uw_client_mod

    monkeypatch.setattr(uw_client_mod, "UWClient", lambda *a, **k: object())

    app = _build_app(
        tmp_path,
        _fake_runner(),
        portfolio={"positions": [{"ticker": "TSLA"}]},
    )
    client = TestClient(app)
    # Seed the cache, then poison oi_baseline with a stale date.
    client.get("/uw-analyze/portfolio")
    cache = routes_mod._portfolio_cache
    cache._entries["TSLA"]["oi_baseline"] = {
        "data_date": "2000-01-01",
        "changes": [{"label": "stale"}],
    }
    persist_count = {"n": 0}
    real_persist = cache._persist

    async def counting_persist():
        persist_count["n"] += 1
        await real_persist()

    monkeypatch.setattr(cache, "_persist", counting_persist)

    body = client.get("/uw-analyze/portfolio").json()
    row = next(r for r in body["tickers"] if r["ticker"] == "TSLA")
    assert row["oi_changes"], "stale baseline should have been refreshed"
    assert row["oi_changes"][0]["label"].startswith("+800 puts")
    today_iso = now_et_date().isoformat()
    assert cache._entries["TSLA"]["oi_baseline"]["data_date"] == today_iso
    assert calls["n"] >= 1
    assert persist_count["n"] >= 1, "refreshed baseline must be persisted"


def test_portfolio_user_initiated_query_param_refreshes_oi_during_closed_market(monkeypatch, tmp_path):
    """Manual refresh during closed hours must also refresh OI baseline.

    Regression for silly-humming-tide.md review finding #1 (Codex + Gemini
    unanimous). POST /uw-analyze/refresh freshens the main snapshot via
    get_or_run directly, but the follow-up SSE GET runs _process_ticker,
    which owns the OI-fetch path. Without a ``user_initiated=1`` query
    param on the follow-up GET, the OI gate still blocks and leaves
    oi_changes stale even though the user explicitly clicked refresh.
    """
    from api.services import uw_analyze_oi_tracker
    from api.services.uw_analyze_oi_tracker import OiChange

    calls = {"n": 0}

    async def fake_fetch(client, ticker, spot):
        calls["n"] += 1
        return [
            OiChange(
                strike=300.0,
                side="call",
                prev_oi=50,
                curr_oi=500,
                delta=450,
                delta_pct=9.0,
                label="+450 calls @ $300 (+900%)",
            )
        ]

    monkeypatch.setattr(uw_analyze_oi_tracker, "fetch_and_diff", fake_fetch)
    import xenon.clients.uw_client as uw_client_mod

    monkeypatch.setattr(uw_client_mod, "UWClient", lambda *a, **k: object())

    app = _build_app(
        tmp_path,
        _fake_runner(),
        portfolio={"positions": [{"ticker": "TSLA"}]},
        market_open=True,
    )
    client = TestClient(app)
    client.get("/uw-analyze/portfolio")  # seed cache

    cache = routes_mod._portfolio_cache
    cache._entries["TSLA"]["oi_baseline"] = {
        "data_date": "2000-01-01",
        "changes": [],
    }
    calls["n"] = 0
    cache._market_open_fn = lambda: False

    # Baseline regression: default closed-market GET gates OI.
    client.get("/uw-analyze/portfolio")
    assert calls["n"] == 0, "default closed-market GET must gate OI"

    # With user_initiated=1, the OI fetch runs despite the closed market.
    body = client.get("/uw-analyze/portfolio?user_initiated=1").json()
    assert calls["n"] == 1, "user_initiated=1 must unblock fetch_and_diff during closed hours"
    row = next(r for r in body["tickers"] if r["ticker"] == "TSLA")
    assert row["oi_changes"], "refreshed OI must appear in the row"
    assert row["oi_changes"][0]["label"].startswith("+450 calls")


def test_portfolio_skips_oi_refresh_during_closed_market(monkeypatch, tmp_path):
    """Regression for the OI-fetch leak (silly-humming-tide.md §2a, Codex C1).

    When the market is closed, automatic /portfolio polls must NOT trigger
    `uw_analyze_oi_tracker.fetch_and_diff`. Before this fix, Monday morning
    after a weekend would hit this path for every ticker because
    `oi_baseline.data_date` was stamped with Friday's ET date — burning
    hundreds of UW calls outside market hours.
    """
    from api.services import uw_analyze_oi_tracker

    calls = {"n": 0}

    async def fake_fetch(client, ticker, spot):
        calls["n"] += 1
        return []

    monkeypatch.setattr(uw_analyze_oi_tracker, "fetch_and_diff", fake_fetch)
    import xenon.clients.uw_client as uw_client_mod

    monkeypatch.setattr(uw_client_mod, "UWClient", lambda *a, **k: object())

    # Build the app in OPEN mode first so we can seed a cache entry for TSLA.
    # Then flip the cache's market_open_fn to CLOSED and poison the OI
    # baseline with an ancient date — the next /portfolio GET must NOT
    # fetch OI.
    app = _build_app(
        tmp_path,
        _fake_runner(),
        portfolio={"positions": [{"ticker": "TSLA"}]},
        market_open=True,
    )
    client = TestClient(app)
    client.get("/uw-analyze/portfolio")  # seed cache

    cache = routes_mod._portfolio_cache
    cache._entries["TSLA"]["oi_baseline"] = {
        "data_date": "2000-01-01",  # extremely stale
        "changes": [],
    }
    # Reset call counter so we only count fetches from the closed-market GET.
    calls["n"] = 0
    # Flip to CLOSED. Automatic /portfolio polls should now be gated and
    # must NOT hit the OI tracker.
    cache._market_open_fn = lambda: False

    body = client.get("/uw-analyze/portfolio").json()
    assert calls["n"] == 0, f"closed-market /portfolio must not call fetch_and_diff, but got {calls['n']} call(s)"
    # The baseline should still read as the poisoned stale value — we did
    # not refresh it.
    assert cache._entries["TSLA"]["oi_baseline"]["data_date"] == "2000-01-01"


def test_portfolio_runs_tickers_concurrently(tmp_path, monkeypatch):
    """Confirm /portfolio fans out per-ticker work with asyncio.gather so the
    cache's Semaphore(3) actually caps concurrency."""
    in_flight = {"n": 0, "peak": 0}

    async def slow_runner(ticker: str):
        in_flight["n"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["n"])
        await asyncio.sleep(0.05)
        in_flight["n"] -= 1
        report = {
            "ticker": ticker,
            "price": 100.0,
            "regime": {"gex_sign": "POSITIVE"},
            "scores": {},
        }
        display = {
            "iv_rank": 50.0,
            "max_pain": 99.0,
            "net_call_premium": 0,
            "net_put_premium": 0,
        }
        return report, display, []

    app = _build_app(
        tmp_path,
        slow_runner,
        portfolio={"positions": [{"ticker": f"T{i}"} for i in range(5)]},
    )
    client = TestClient(app)
    resp = client.get("/uw-analyze/portfolio")
    assert resp.status_code == 200
    # 5 portfolio tickers + static universe tickers
    assert len(resp.json()["tickers"]) >= 5
    assert in_flight["peak"] >= 2, f"expected concurrent runs, peak={in_flight['peak']}"


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
    assert r.json()["refreshed"] >= 2
    assert "AAPL" in calls and "NVDA" in calls


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


def test_legacy_post_uses_unified_cache(tmp_path):
    """Two sequential POSTs for the same ticker within the cache TTL should
    collapse to a single runner call, and populate UwAnalyzeCache — proving
    the legacy in-process _cache is gone."""
    call_count = {"n": 0}

    async def counting_runner(ticker: str):
        call_count["n"] += 1
        base = _fake_runner()
        return await base(ticker)

    app = _build_app(tmp_path, counting_runner)
    client = TestClient(app)

    r1 = client.post("/uw-analyze", json={"ticker": "AAPL"})
    r2 = client.post("/uw-analyze", json={"ticker": "AAPL"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert call_count["n"] == 1, "second POST must be served from UwAnalyzeCache"

    # Most direct proof: the unified cache now has an entry for AAPL.
    assert "AAPL" in routes_mod.get_portfolio_cache().all_entries()


# ── On-demand OI refresh hotspot regressions ──────────────────────────────
#
# Three regressions locking in the fix for the "preload not working / 400%
# CPU / fan spinning" incident. Root cause was per-ticker `_persist()` +
# unbounded UWClient instantiation + unbounded `fetch_and_diff` fan-out
# inside `asyncio.gather`. See docs/plans/stateless-scribbling-meadow.md.


class _FakeOiChange:
    def __init__(self, strike: float) -> None:
        self.strike = strike

    def to_dict(self) -> dict:
        return {
            "strike": self.strike,
            "side": "call",
            "delta": 1,
            "label": f"+1 calls @ ${self.strike:g}",
        }


def _install_oi_stub(monkeypatch, *, tracker_fn):
    """Point route-level on-demand OI refresh at `tracker_fn`.

    The route imports `api.services.uw_analyze_oi_tracker` lazily inside
    `_process`, so we monkeypatch the module attribute.
    """
    from api.services import uw_analyze_oi_tracker

    monkeypatch.setattr(uw_analyze_oi_tracker, "fetch_and_diff", tracker_fn)


def _clear_static_universe(monkeypatch):
    """Drop the always-on scaffold universe for focused regression tests.
    Without this, 21 macro tickers (SPY, QQQ, …) would also run through
    `get_or_run` and inflate persist / UWClient counts."""
    monkeypatch.setattr(cand, "UW_ANALYZE_STATIC_UNIVERSE", frozenset())


def _seed_stale_entries(tickers):
    """Populate the route cache singleton with entries whose oi_baseline
    is stamped to yesterday, forcing the on-demand refresh branch."""
    cache = routes_mod.get_portfolio_cache()
    yesterday = "1970-01-01"  # any date != today — forces refresh branch
    for t in tickers:
        cache._entries[t] = {
            "current": {
                "ticker": t,
                "ts": "2099-01-01T00:00:00+00:00",  # far future → TTL fresh
                "report": {},
                "display": {},
                "flow_alerts": [],
                "derived": {"spot": 100.0},
            },
            "previous": None,
            "oi_baseline": {"data_date": yesterday, "changes": []},
            # Must match what `seed_candidates` will attach, otherwise
            # `get_or_run` mutates sources on first warm hit and persists.
            "sources": ["portfolio"],
            "materialized_changes": [],
        }
    cache._loaded = True


def test_portfolio_persists_once_per_request(tmp_path, monkeypatch):
    """Regression: the route must call `cache._persist()` at most ONCE per
    /portfolio GET, no matter how many tickers trigger on-demand OI refresh.
    The previous bug rewrote the 2.3MB cache file N times per request.
    """
    tickers = [f"T{i}" for i in range(5)]
    app = _build_app(
        tmp_path,
        _fake_runner(),
        portfolio={"positions": [{"ticker": t} for t in tickers]},
    )
    _clear_static_universe(monkeypatch)
    _seed_stale_entries(tickers)

    async def fake_fetch_and_diff(client, ticker, spot):
        return [_FakeOiChange(100.0)]

    _install_oi_stub(monkeypatch, tracker_fn=fake_fetch_and_diff)

    cache = routes_mod.get_portfolio_cache()
    persist_count = {"n": 0}
    orig_persist = cache._persist

    async def counting_persist(*a, **kw):
        persist_count["n"] += 1
        return await orig_persist(*a, **kw)

    monkeypatch.setattr(cache, "_persist", counting_persist)

    # Stub out UWClient so no real network.
    class _StubUW:
        pass

    monkeypatch.setattr(routes_mod, "_shared_uw_client", lambda: _StubUW())

    r = TestClient(app).get("/uw-analyze/portfolio")
    assert r.status_code == 200
    assert persist_count["n"] == 1, f"expected exactly 1 cache persist per /portfolio GET, got {persist_count['n']}"
    # Internal flag must not leak to the response.
    for row in r.json()["tickers"]:
        assert "_oi_refreshed" not in row


def test_on_demand_oi_fanout_is_bounded(tmp_path, monkeypatch):
    """Regression: on-demand `fetch_and_diff` concurrency must be bounded
    by `_ON_DEMAND_OI_SEM` (=3). Unbounded fan-out previously let 35
    tickers hit UW simultaneously on the first warm-cache call."""
    tickers = [f"T{i}" for i in range(10)]
    app = _build_app(
        tmp_path,
        _fake_runner(),
        portfolio={"positions": [{"ticker": t} for t in tickers]},
    )
    _clear_static_universe(monkeypatch)
    _seed_stale_entries(tickers)

    inflight = {"current": 0, "peak": 0}

    async def fake_fetch_and_diff(client, ticker, spot):
        inflight["current"] += 1
        inflight["peak"] = max(inflight["peak"], inflight["current"])
        # Yield control so other coroutines queued behind the semaphore
        # have a chance to increment before we decrement.
        await asyncio.sleep(0.01)
        inflight["current"] -= 1
        return [_FakeOiChange(100.0)]

    _install_oi_stub(monkeypatch, tracker_fn=fake_fetch_and_diff)
    monkeypatch.setattr(routes_mod, "_shared_uw_client", lambda: object())

    r = TestClient(app).get("/uw-analyze/portfolio")
    assert r.status_code == 200
    # The module-level semaphore is 3; observed peak must not exceed that.
    assert inflight["peak"] <= 3, f"on-demand OI concurrency unbounded: peak={inflight['peak']}"
    # Sanity: all tickers did go through the refresh branch.
    assert inflight["peak"] >= 1


def test_shared_uw_client_reused_across_tickers(tmp_path, monkeypatch):
    """Regression: `_shared_uw_client()` must be invoked at most once per
    /portfolio GET regardless of ticker count. The previous bug allocated
    a new `UWClient()` per ticker inside the gather loop."""
    tickers = [f"T{i}" for i in range(5)]
    app = _build_app(
        tmp_path,
        _fake_runner(),
        portfolio={"positions": [{"ticker": t} for t in tickers]},
    )
    _clear_static_universe(monkeypatch)
    _seed_stale_entries(tickers)

    ctor_count = {"n": 0}

    class _StubUW:
        def __init__(self):
            ctor_count["n"] += 1

    # Force a fresh construction by clearing any prior singleton and
    # pointing UWClient at our stub.
    routes_mod._uw_client_singleton = None
    monkeypatch.setattr("xenon.clients.uw_client.UWClient", _StubUW)

    async def fake_fetch_and_diff(client, ticker, spot):
        # Assert every call got the same client instance.
        assert client is routes_mod._uw_client_singleton
        return []

    _install_oi_stub(monkeypatch, tracker_fn=fake_fetch_and_diff)

    r = TestClient(app).get("/uw-analyze/portfolio")
    assert r.status_code == 200
    assert ctor_count["n"] == 1, f"UWClient constructed {ctor_count['n']} times; expected 1 shared instance"


def test_portfolio_survives_persist_failure(tmp_path, monkeypatch):
    """Regression: if the batched post-gather `_persist()` raises (e.g.
    transient FS error), the /portfolio GET must still return 200 with
    the in-memory refreshed baseline. The previous per-ticker persist
    was wrapped in a try/except; the batched version must preserve that
    robustness."""
    tickers = [f"T{i}" for i in range(3)]
    app = _build_app(
        tmp_path,
        _fake_runner(),
        portfolio={"positions": [{"ticker": t} for t in tickers]},
    )
    _clear_static_universe(monkeypatch)
    _seed_stale_entries(tickers)

    async def fake_fetch_and_diff(client, ticker, spot):
        return [_FakeOiChange(100.0)]

    _install_oi_stub(monkeypatch, tracker_fn=fake_fetch_and_diff)
    monkeypatch.setattr(routes_mod, "_shared_uw_client", lambda: object())

    cache = routes_mod.get_portfolio_cache()

    async def exploding_persist(*a, **kw):
        raise OSError("simulated disk-full")

    monkeypatch.setattr(cache, "_persist", exploding_persist)

    r = TestClient(app).get("/uw-analyze/portfolio")
    assert r.status_code == 200, f"persist failure must not 500 the route; got {r.status_code}: {r.text}"
    # In-memory baseline was still updated, which is what matters for
    # this request and subsequent ones this process serves.
    body = r.json()
    assert len(body["tickers"]) == 3
