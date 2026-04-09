"""Tests for POST /uw-analyze (scripts/api/routes/uw_analyze.py).

Mocks `run_analysis_with_data` so tests run without UW network access.
Covers:
  1. Happy path → 200 with report + display blocks matching DTO shape
  2. Unknown ticker (UWNotFoundError) → 404
  3. Upstream UW error (UWAPIError) → 502
  4. Bad ticker (ValueError) → 400
  5. Route runs the blocking call via threadpool (asyncio.to_thread)
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ["XENON_TEST_MODE"] = "1"
os.environ["XENON_API_TEST_MODE"] = "1"

from analysis.models import (  # noqa: E402
    AnalysisReport,
    BenchmarkContext,
    BenchmarkSnapshot,
    BucketScores,
    RegimeState,
    TickerData,
    VRPState,
)
from api.services.uw_analyze_cache import UwAnalyzeCache  # noqa: E402
from api.services.uw_analyze_flow_tracker import FlowLog  # noqa: E402
from clients.uw_client import UWAPIError, UWNotFoundError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"


def _make_fixtures() -> tuple[AnalysisReport, TickerData]:
    td = TickerData(
        ticker="AAPL",
        price=184.22,
        fetched_at=datetime(2026, 4, 8, 14, 2, 11),
        gex={"net": 1.0},
        gex_by_strike={
            "190": {"call_gamma": 44.8, "put_gamma": -2.7},
            "185": {"call_gamma": 14.2, "put_gamma": -4.5},
            "180": {"call_gamma": 3.1, "put_gamma": -9.4},
        },
        iv=22.0,
        rv=18.6,
        iv_percentile=38.0,
        # Real UW /volatility/term-structure shape: "volatility" as a
        # stringly-typed IV, ordered front→back by dte. Previous fixture
        # used "iv" which doesn't exist in the API response — that's the
        # bug that made term_structure_label always None in production.
        term_structure=[
            {"dte": 1, "volatility": "0.22"},
            {"dte": 981, "volatility": "0.28"},
        ],
        rr_skew_25d=None,
        vrp_history=None,
        flow_alerts=None,
        net_premium=None,
        pcr=None,
        darkpool=None,
        oi_changes=None,
        short_interest=None,
        earnings_date=date(2026, 5, 1),
        earnings_within_14d=False,
        iv_rank=38.0,
        iv_52w_low=12.0,
        iv_52w_high=55.0,
        net_call_premium=12_400_000.0,
        net_put_premium=-3_100_000.0,
        short_volume_ratio=0.41,
        short_volume_trend=[0.40, 0.41, 0.42],
        call_wall_strike=190.0,
        call_wall_gamma=44.8,
        put_wall_strike=175.0,
        put_wall_gamma=-26.7,
        gamma_per_1pct=42_000_000.0,
        sector="XLK",
    )
    vrp = VRPState(
        vrp_raw=0.04,
        vrp_zscore=1.2,
        iv_percentile=38.0,
        ts_ratio=1.05,
        ts_inverted=False,
        earnings_within_14d=False,
        data_freshness="live",
    )
    regime = RegimeState(
        regime="R1",
        reason="positive gex; flip below price",
        gex_sign="positive",
        gex_flip_relative="below_price",
        flip_distance_pct=-1.1,
    )
    scores = BucketScores(
        market_structure=24.0,
        volatility=19.0,
        flow=17.0,
        positioning=0.0,
        composite=15.0,
        grade="B",
        bias="MIXED",
        mode="full",
        reweighted=True,
        skipped_buckets=["positioning"],
    )
    benchmark = BenchmarkContext(
        spy=BenchmarkSnapshot(
            ticker="SPY",
            iv_rank=22.0,
            gex_regime="positive",
            gex_flip=None,
            price=520.0,
            data_date="2026-04-08",
            freshness="live",
        ),
        sector_etf=BenchmarkSnapshot(
            ticker="XLK",
            iv_rank=31.0,
            gex_regime="mixed",
            gex_flip=None,
            price=210.0,
            data_date="2026-04-08",
            freshness="live",
        ),
    )
    report = AnalysisReport(
        ticker="AAPL",
        price=184.22,
        fetched_at=td.fetched_at.isoformat(),
        data_freshness={"gex": "live", "volatility": "live", "earnings": "live", "benchmark_spy": "live"},
        benchmark=benchmark,
        vrp=vrp,
        regime=regime,
        scores=scores,
        notes=["positioning bucket unavailable"],
        setup_thesis={
            "bias": "MIXED",
            "regime": "R1",
            "structure_family": "neutral",
            "rationale": "demo",
        },
    )
    return report, td


@pytest.fixture(autouse=True)
def _clear_cache(tmp_path) -> None:
    from api.routes import uw_analyze as route_mod  # noqa: WPS433

    route_mod.reset_state_for_tests()
    route_mod._portfolio_cache = UwAnalyzeCache(
        cache_path=tmp_path / "uw-analyze-cache.json",
        market_open_fn=lambda: True,
    )
    route_mod._flow_log = FlowLog(path=tmp_path / "uw-analyze-flow.json")
    yield
    route_mod.reset_state_for_tests()


@pytest.fixture()
def client() -> TestClient:
    from api import server  # noqa: WPS433

    return TestClient(server.app)


def test_happy_path_returns_report_and_display(client: TestClient) -> None:
    report, td = _make_fixtures()
    with patch("api.routes.uw_analyze.run_analysis_with_data", return_value=(report, td)) as m:
        resp = client.post("/uw-analyze", json={"ticker": "aapl"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body.keys()) == {"report", "display", "generated_at"}
    assert body["report"]["ticker"] == "AAPL"
    assert body["report"]["scores"]["composite"] == 15.0
    d = body["display"]
    assert d["sector"] == "XLK"
    assert d["iv_rank"] == 38.0
    assert d["call_wall_strike"] == 190.0
    assert d["put_wall_strike"] == 175.0
    assert d["gamma_per_1pct"] == 42_000_000.0
    assert d["net_call_premium"] == 12_400_000.0
    assert d["term_structure_label"] == "normal"
    rows = d["gex_by_strike"]
    assert isinstance(rows, list) and len(rows) >= 1
    # Sorted descending by strike
    strikes = [row["strike"] for row in rows]
    assert strikes == sorted(strikes, reverse=True)
    # Wall flag set on the matching strike
    wall_row = next(r for r in rows if r["strike"] == 190.0)
    assert wall_row["is_call_wall"] is True
    assert wall_row["distance_pct"] is not None
    # Mock called with uppercased ticker
    m.assert_called_once()
    assert m.call_args.args[0] == "AAPL"


def test_unknown_ticker_returns_404(client: TestClient) -> None:
    def _raise(*_a, **_kw):
        raise UWNotFoundError("nope", status_code=404, response_body="")

    with patch("api.routes.uw_analyze.run_analysis_with_data", side_effect=_raise):
        resp = client.post("/uw-analyze", json={"ticker": "ZZZZ"})
    assert resp.status_code == 404


def test_upstream_uw_error_returns_502(client: TestClient) -> None:
    def _raise(*_a, **_kw):
        raise UWAPIError("upstream boom")

    with patch("api.routes.uw_analyze.run_analysis_with_data", side_effect=_raise):
        resp = client.post("/uw-analyze", json={"ticker": "AAPL"})
    assert resp.status_code == 502


def test_bad_ticker_returns_400(client: TestClient) -> None:
    resp = client.post("/uw-analyze", json={"ticker": ""})
    assert resp.status_code == 400


def test_gex_table_built_from_real_strikes_shape(client: TestClient) -> None:
    """Real ticker_data.py shape: {"strikes": [{strike, gamma, call_gamma, put_gamma}]}."""
    report, td = _make_fixtures()
    # Replace dict-keyed shape with the actual normalized list shape.
    real_shape = {
        "strikes": [
            {"strike": 190.0, "gamma": 42.1, "call_gamma": 44.8, "put_gamma": -2.7},
            {"strike": 185.0, "gamma": 9.7, "call_gamma": 14.2, "put_gamma": -4.5},
            {"strike": 180.0, "gamma": -6.3, "call_gamma": 3.1, "put_gamma": -9.4},
            {"strike": 175.0, "gamma": -24.8, "call_gamma": 1.9, "put_gamma": -26.7},
        ],
    }
    from dataclasses import replace

    td2 = replace(td, gex_by_strike=real_shape)
    with patch("api.routes.uw_analyze.run_analysis_with_data", return_value=(report, td2)):
        resp = client.post("/uw-analyze", json={"ticker": "AAPL"})
    assert resp.status_code == 200
    rows = resp.json()["display"]["gex_by_strike"]
    assert len(rows) == 4
    assert [r["strike"] for r in rows] == [190.0, 185.0, 180.0, 175.0]
    wall = next(r for r in rows if r["strike"] == 190.0)
    assert wall["is_call_wall"] is True
    assert wall["call_gamma"] == 44.8
    assert wall["put_gamma"] == -2.7
    assert wall["net_gamma"] == 42.1


def test_route_imports_with_scripts_only_pythonpath() -> None:
    """Legacy scripts tests run from scripts/tests with only scripts/ on PYTHONPATH."""
    code = "from api.routes import uw_analyze; print(uw_analyze.__file__)"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SCRIPTS_DIR)

    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT / "scripts" / "tests",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr


def test_route_uses_threadpool(client: TestClient) -> None:
    """Runner must call run_analysis_with_data via asyncio.to_thread (non-blocking)."""
    report, td = _make_fixtures()
    calls: list[tuple] = []

    async def fake_to_thread(func, *args, **kwargs):
        calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    from api.routes import uw_analyze as route_mod  # noqa: WPS433

    with (
        patch("api.routes.uw_analyze.asyncio.to_thread", side_effect=fake_to_thread),
        patch("api.routes.uw_analyze.run_analysis_with_data", return_value=(report, td)) as mocked,
    ):
        report_dict, display_dict, flow_alerts = asyncio.run(route_mod._runner("AAPL"))
        assert len(calls) == 1
        assert calls[0][0] is mocked
        assert report_dict["ticker"] == "AAPL"
        assert display_dict["sector"] == "XLK"
        assert flow_alerts == []
