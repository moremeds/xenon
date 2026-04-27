"""Backfill uw_analyze_snapshots from on-disk uw_analyze_history archives."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select, text

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import uw_analyze_gex_strikes, uw_analyze_snapshots


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


SAMPLE_ARCHIVE = {
    "current": {
        "ticker": "AAPL",
        "ts": "2026-04-08T14:03:48.800154+00:00",
        "report": {
            "ticker": "AAPL",
            "price": 184.22,
            "fetched_at": "2026-04-08T14:02:11+00:00",
            "scores": {"composite": 15.0, "flow": 17.0, "grade": "B", "bias": "MIXED"},
            "regime": {"regime": "R1", "gex_sign": "positive"},
            "vrp": {"vrp_zscore": 1.2, "iv_percentile": 38.0},
        },
        "display": {
            "iv": 22.0,
            "iv_rank": 38.0,
            "call_wall_strike": 190.0,
            "gex_by_strike": [
                {
                    "strike": 190.0,
                    "call_gamma": 44.8,
                    "put_gamma": -2.7,
                    "net_gamma": 42.1,
                    "distance_pct": 0.03,
                    "is_call_wall": True,
                    "is_put_wall": False,
                },
            ],
        },
        "derived": {"gex_sign": "POSITIVE", "spot": 184.22},
        "dark_pool_summary": {"signal": "NONE", "score": -20.0},
        "options_flow_summary": {"bias": "NO_DATA", "total_alerts": 0},
        "flow_alerts": [],
    },
    "materialized_changes": [],
    "archived_at": "2026-04-08T14:03:48.800469+00:00",
}


def test_backfill_one_file_creates_snapshot_and_strikes(engine, tmp_path):
    aapl_dir = tmp_path / "AAPL"
    aapl_dir.mkdir()
    (aapl_dir / "20260408-140348-800496.json").write_text(json.dumps(SAMPLE_ARCHIVE))

    from scripts.migrations import _2026_04_26_backfill_uw_analyze_history as bf

    n = bf.run(history_root=tmp_path, db_url=_sync_test_db_url())
    assert n == 1

    with engine.begin() as conn:
        snap = conn.execute(select(uw_analyze_snapshots)).first()
        strikes = conn.execute(select(uw_analyze_gex_strikes)).all()
    assert snap.ticker == "AAPL"
    assert snap.report["scores"]["grade"] == "B"
    assert float(snap.price) == 184.22
    assert snap.regime_label == "R1"
    assert float(snap.iv_rank) == 38.0
    assert len(strikes) == 1
    assert float(strikes[0].strike) == 190.0
    assert strikes[0].is_call_wall is True


def test_backfill_idempotent(engine, tmp_path):
    aapl_dir = tmp_path / "AAPL"
    aapl_dir.mkdir()
    (aapl_dir / "f.json").write_text(json.dumps(SAMPLE_ARCHIVE))
    from scripts.migrations import _2026_04_26_backfill_uw_analyze_history as bf

    bf.run(history_root=tmp_path, db_url=_sync_test_db_url())
    bf.run(history_root=tmp_path, db_url=_sync_test_db_url())
    with engine.begin() as conn:
        cnt = conn.execute(text("SELECT count(*) FROM xenon.uw_analyze_snapshots")).scalar()
    assert cnt == 1, "second run should not duplicate"
