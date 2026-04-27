"""GEX scanner dual-writes to scan_results and gex_snapshots."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.queries.scans import save_gex_snapshot
from xenon.db.schema import gex_snapshots


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


SAMPLE_GEX = {
    "ticker": "AAPL",
    "spot": 184.22,
    "net_gex": 12345.67,
    "net_dex": -890.12,
    "vol_pc": 0.85,
    "iv": {"iv30d": 0.22, "iv_rank": 38.0, "hv30": 0.186, "mq_iv30d": 0.21},
    "levels": {
        "max_magnet": {"strike": 185.0, "gamma": 100.0, "distance": 0.78, "distance_pct": 0.42},
        "second_magnet": {"strike": 180.0, "gamma": 80.0, "distance": 4.22, "distance_pct": 2.29},
        "max_accelerator": {"strike": 195.0, "gamma": 50.0, "distance": 10.78, "distance_pct": 5.85},
        "put_wall": {"strike": 175.0, "gamma": -90.0, "distance": -9.22, "distance_pct": -5.0},
    },
    "data_date": "2026-04-26",
}


def test_save_gex_snapshot_writes_row(engine):
    with engine.begin() as conn:
        new_id = save_gex_snapshot(conn, payload=SAMPLE_GEX)
        row = conn.execute(select(gex_snapshots).where(gex_snapshots.c.id == new_id)).first()
    assert row.ticker == "AAPL"
    assert float(row.spot) == 184.22
    assert float(row.net_gex) == 12345.67
    assert float(row.iv_30d) == 0.22
    assert float(row.iv_rank) == 38.0
    assert float(row.level_max_magnet_strike) == 185.0
    assert float(row.level_put_wall_strike) == 175.0
    assert row.data_date.isoformat() == "2026-04-26"


def test_save_gex_snapshot_preserves_full_payload(engine):
    with engine.begin() as conn:
        new_id = save_gex_snapshot(conn, payload=SAMPLE_GEX)
        row = conn.execute(select(gex_snapshots).where(gex_snapshots.c.id == new_id)).first()
    assert row.payload == SAMPLE_GEX
