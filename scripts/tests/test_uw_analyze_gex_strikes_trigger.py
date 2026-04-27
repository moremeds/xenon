"""display.gex_by_strike JSONB array fans out to uw_analyze_gex_strikes."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, insert, select

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import uw_analyze_gex_strikes, uw_analyze_snapshots


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def test_gex_by_strike_fans_out(engine):
    display = {
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
            {
                "strike": 185.0,
                "call_gamma": 14.2,
                "put_gamma": -4.5,
                "net_gamma": 9.7,
                "distance_pct": 0.0042,
                "is_call_wall": False,
                "is_put_wall": False,
            },
            {
                "strike": 175.0,
                "call_gamma": 3.1,
                "put_gamma": -9.4,
                "net_gamma": -6.3,
                "distance_pct": -0.0500,
                "is_call_wall": False,
                "is_put_wall": True,
            },
        ],
    }
    with engine.begin() as conn:
        result = conn.execute(
            insert(uw_analyze_snapshots).values(ticker="AAPL", display=display).returning(uw_analyze_snapshots.c.id)
        )
        snap_id = result.scalar()
        rows = conn.execute(
            select(uw_analyze_gex_strikes)
            .where(uw_analyze_gex_strikes.c.snapshot_id == snap_id)
            .order_by(uw_analyze_gex_strikes.c.strike.desc())
        ).all()
    assert len(rows) == 3
    assert [float(r.strike) for r in rows] == [190.0, 185.0, 175.0]
    assert rows[0].is_call_wall is True
    assert rows[2].is_put_wall is True
    assert float(rows[0].net_gamma) == 42.1


def test_no_gex_by_strike_no_children(engine):
    with engine.begin() as conn:
        result = conn.execute(
            insert(uw_analyze_snapshots)
            .values(ticker="AAPL", display={"iv_rank": 50.0})
            .returning(uw_analyze_snapshots.c.id)
        )
        snap_id = result.scalar()
        rows = conn.execute(select(uw_analyze_gex_strikes).where(uw_analyze_gex_strikes.c.snapshot_id == snap_id)).all()
    assert rows == []
