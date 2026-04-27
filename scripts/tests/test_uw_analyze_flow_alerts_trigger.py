"""flow_alerts JSONB array fans out to uw_analyze_flow_alerts child table."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, insert, select, text

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import uw_analyze_flow_alerts, uw_analyze_snapshots


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def test_flow_alerts_array_fans_out(engine):
    alerts = [
        {"type": "dark_pool_accumulation", "severity": "high", "size": 5_000_000},
        {"type": "deep_conviction_flow", "severity": "medium", "premium": 1_200_000},
    ]
    with engine.begin() as conn:
        result = conn.execute(
            insert(uw_analyze_snapshots).values(ticker="TSLA", flow_alerts=alerts).returning(uw_analyze_snapshots.c.id)
        )
        snap_id = result.scalar()
        rows = conn.execute(select(uw_analyze_flow_alerts).where(uw_analyze_flow_alerts.c.snapshot_id == snap_id)).all()
    assert len(rows) == 2
    by_type = {r.alert_type: r for r in rows}
    assert by_type["dark_pool_accumulation"].alert_severity == "high"
    assert by_type["dark_pool_accumulation"].alert_payload["size"] == 5_000_000
    assert by_type["deep_conviction_flow"].alert_severity == "medium"


def test_flow_alerts_null_does_not_fan_out(engine):
    with engine.begin() as conn:
        result = conn.execute(insert(uw_analyze_snapshots).values(ticker="TSLA").returning(uw_analyze_snapshots.c.id))
        snap_id = result.scalar()
        count = conn.execute(
            text("SELECT count(*) FROM xenon.uw_analyze_flow_alerts WHERE snapshot_id=:i"),
            {"i": snap_id},
        ).scalar()
    assert count == 0


def test_flow_alerts_update_replaces_children(engine):
    """Updating flow_alerts should refresh the child rows (no duplicates)."""
    with engine.begin() as conn:
        result = conn.execute(
            insert(uw_analyze_snapshots)
            .values(ticker="TSLA", flow_alerts=[{"type": "a", "severity": "low"}])
            .returning(uw_analyze_snapshots.c.id)
        )
        snap_id = result.scalar()
        conn.execute(
            text("UPDATE xenon.uw_analyze_snapshots SET flow_alerts = CAST(:a AS jsonb) WHERE id = :i"),
            {
                "a": '[{"type":"b","severity":"high"},{"type":"c","severity":"medium"}]',
                "i": snap_id,
            },
        )
        rows = conn.execute(select(uw_analyze_flow_alerts).where(uw_analyze_flow_alerts.c.snapshot_id == snap_id)).all()
    types = sorted(r.alert_type for r in rows)
    assert types == ["b", "c"], f"expected refreshed [b,c], got {types}"
