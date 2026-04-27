"""display.short_volume_trend array fans out to uw_analyze_short_volume_trend."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, insert, select

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import uw_analyze_short_volume_trend, uw_analyze_snapshots


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def test_short_volume_trend_fans_out(engine):
    with engine.begin() as conn:
        result = conn.execute(
            insert(uw_analyze_snapshots)
            .values(ticker="NVDA", display={"short_volume_trend": [0.40, 0.41, 0.42]})
            .returning(uw_analyze_snapshots.c.id)
        )
        snap_id = result.scalar()
        rows = conn.execute(
            select(uw_analyze_short_volume_trend)
            .where(uw_analyze_short_volume_trend.c.snapshot_id == snap_id)
            .order_by(uw_analyze_short_volume_trend.c.position_in_trend)
        ).all()
    assert [r.position_in_trend for r in rows] == [0, 1, 2]
    assert [float(r.ratio) for r in rows] == [0.40, 0.41, 0.42]
