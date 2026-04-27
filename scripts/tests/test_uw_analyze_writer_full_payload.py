"""Verify uw_analyze archive writer persists the full payload."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select

from scripts.tests.conftest import _sync_test_db_url
from xenon.api.services.uw_analyze_cache import UwAnalyzeCache
from xenon.db.schema import uw_analyze_snapshots


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


SAMPLE_CURRENT = {
    "ticker": "AAPL",
    "ts": "2026-04-08T14:03:48.800154+00:00",
    "report": {
        "ticker": "AAPL",
        "price": 184.22,
        "fetched_at": "2026-04-08T14:02:11+00:00",
        "benchmark": {
            "spy": {"ticker": "SPY", "iv_rank": 22.0, "gex_regime": "positive"},
            "sector_etf": {"ticker": "XLK", "iv_rank": 31.0, "gex_regime": "mixed"},
        },
        "vrp": {
            "vrp_raw": 0.04,
            "vrp_zscore": 1.2,
            "iv_percentile": 38.0,
            "ts_ratio": 1.05,
            "ts_inverted": False,
            "earnings_within_14d": False,
        },
        "regime": {
            "regime": "R1",
            "reason": "demo",
            "gex_sign": "positive",
            "gex_flip_relative": "below_price",
            "flip_distance_pct": -1.1,
        },
        "scores": {
            "market_structure": 24.0,
            "volatility": 19.0,
            "flow": 17.0,
            "positioning": 0.0,
            "composite": 15.0,
            "grade": "B",
            "bias": "MIXED",
            "mode": "full",
            "reweighted": True,
        },
    },
    "display": {
        "iv_rank": 38.0,
        "iv": 22.0,
        "rv": 18.6,
        "call_wall_strike": 190.0,
        "put_wall_strike": 175.0,
        "gamma_per_1pct": 42000000.0,
        "net_call_premium": 12400000.0,
        "net_put_premium": -3100000.0,
        "short_volume_ratio": 0.41,
        "short_volume_trend": [0.4, 0.41, 0.42],
        "term_structure_label": "normal",
        "max_pain": None,
        "gex_by_strike": [
            {
                "strike": 190.0,
                "call_gamma": 44.8,
                "put_gamma": -2.7,
                "net_gamma": 42.1,
                "distance_pct": 0.0314,
                "is_call_wall": True,
                "is_put_wall": False,
            },
        ],
    },
    "flow_alerts": [],
    "derived": {"gex_sign": "POSITIVE", "spot": 184.22},
    "dark_pool_summary": {
        "score": -20.0,
        "signal": "NONE",
        "direction": "NO_DATA",
        "strength": 0,
        "buy_ratio": None,
        "options_conflict": False,
        "num_prints": 0,
        "sustained_days": 0,
    },
    "options_flow_summary": {
        "total_alerts": 0,
        "total_premium": 0,
        "call_premium": 0,
        "put_premium": 0,
        "call_put_ratio": None,
        "bias": "NO_DATA",
    },
}


def test_archive_to_postgres_writes_full_payload(engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", _sync_test_db_url())
    UwAnalyzeCache._archive_to_postgres(
        ticker="AAPL",
        current=SAMPLE_CURRENT,
        materialized_changes=[],
        archived_at_iso="2026-04-08T14:03:48.800469+00:00",
    )
    with engine.begin() as conn:
        row = conn.execute(select(uw_analyze_snapshots)).first()
    assert row is not None
    assert row.ticker == "AAPL"
    assert row.report["scores"]["composite"] == 15.0
    assert row.display["call_wall_strike"] == 190.0
    assert row.derived["gex_sign"] == "POSITIVE"
    assert row.dark_pool_summary["signal"] == "NONE"
    assert row.options_flow_summary["bias"] == "NO_DATA"
    assert float(row.price) == 184.22
    assert float(row.composite_score) == 15.0
    assert row.grade == "B"
    assert row.bias == "MIXED"
    assert row.regime_label == "R1"
    assert row.gex_sign == "positive"
    assert float(row.iv) == 22.0
    assert float(row.iv_rank) == 38.0
    assert float(row.call_wall_strike) == 190.0
    assert row.dp_signal == "NONE"
    assert row.of_bias == "NO_DATA"
    assert float(row.spy_iv_rank) == 22.0
    assert row.sector_etf_ticker == "XLK"
    assert row.report_fetched_at is not None
    assert row.archived_at is not None
