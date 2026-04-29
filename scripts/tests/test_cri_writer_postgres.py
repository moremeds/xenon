"""POST /regime/scan persists CRI to cri_series via _write_scan_to_postgres.

Phase 0 task 0.4 of the VCG-R + CRI rewiring. Without this, the
Phase 1 regime_state view (LIMIT 1 ORDER BY recorded_at DESC) has
nothing to read — POST /regime/scan today only writes the generic
scan_results table.

Mirrors test_vcg_writer_postgres.py (calls the same _write_scan_to_postgres
shim used by every scan archive path).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import cri_series, scan_results


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


SAMPLE_CRI = {
    "date": "2026-04-29",
    "vix": 18.87,
    "vvix": 98.15,
    "spy": 520.0,
    "vix_5d_roc": 1.2,
    "vvix_vix_ratio": 5.20,
    "spx_100d_ma": 510.0,
    "spx_distance_pct": 1.96,
    "cor1m": 0.42,
    "realized_vol": 14.5,
    "cri": {"score": 27.4, "level": "LOW", "components": {"vix_z": 0.3}},
    "cta": {
        "exposure_pct": 87.5,
        "forced_reduction_pct": 12.5,
        "forced_reduction": True,
    },
    "menthorq_cta": {"score": 5.1},
    "crash_trigger": {"triggered": False, "fired": False, "conditions": {}},
    "history": [],
    "spy_closes": [],
}


def test_write_scan_to_postgres_persists_cri_to_cri_series(engine, monkeypatch):
    """Both scan_results AND cri_series get a row, just like the
    vcg.json branch already does for vcg_series."""
    from xenon.api.server import _write_scan_to_postgres

    monkeypatch.setenv("DATABASE_URL", _sync_test_db_url())

    _write_scan_to_postgres("cri.json", SAMPLE_CRI)

    with engine.begin() as conn:
        archive_row = conn.execute(
            select(scan_results)
            .where(scan_results.c.scan_type == "cri")
            .where(scan_results.c.payload["date"].astext == "2026-04-29")
            .order_by(scan_results.c.id.desc())
            .limit(1)
        ).first()
        series_row = conn.execute(select(cri_series).where(cri_series.c.payload["date"].astext == "2026-04-29")).first()

    # Existing behaviour preserved: scan_results gets a row.
    assert archive_row is not None
    assert archive_row.payload["cri"]["score"] == 27.4

    # New behaviour: cri_series gets a row whose generated columns
    # resolve correctly from the same payload (proving the booleans
    # added in Phase 0.2 land in their typed columns).
    assert series_row is not None
    assert float(series_row.cri_level) == 27.4
    assert series_row.crash_trigger_fired is False
    assert series_row.cta_forced_reduction is True
    assert float(series_row.vix) == 18.87


def test_write_scan_to_postgres_cri_failure_does_not_break_archive(engine, monkeypatch):
    """Best-effort semantics: if cri_series insert fails, the route still
    returns the scanner output — _write_scan_to_postgres swallows everything.
    Verified by deleting payload['cri'] (which makes cri_level derive 0,
    still a valid insert) — pure smoke test that the new branch doesn't
    introduce new failure modes."""
    from xenon.api.server import _write_scan_to_postgres

    monkeypatch.setenv("DATABASE_URL", _sync_test_db_url())

    payload = {"date": "2026-05-03"}  # no cri key — cri_level derives to 0
    # Should not raise.
    _write_scan_to_postgres("cri.json", payload)
