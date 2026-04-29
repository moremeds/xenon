"""save_cri_scan() persists a CRI scanner JSON payload to cri_series.

Phase 0 task 0.3 of the VCG-R + CRI rewiring. Plan called this
`xenon.scanners.cri.persist()` but the cleaner home is alongside the
existing `save_vcg_scan` / `save_cri_datapoint` helpers in
`xenon.db.queries.scans` — same layering, sync conn, plain INSERT.

The view `regime_state` introduced in Phase 1 will pick the latest
row via ORDER BY recorded_at DESC LIMIT 1, so multiple rows per
calendar day are intentional (30-min scheduled cadence + manual
refreshes). No ON CONFLICT clause — duplicates are not the failure
mode we are guarding against.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.queries.scans import save_cri_scan
from xenon.db.schema import cri_series


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def _full_payload(date: str = "2026-04-29") -> dict:
    return {
        "date": date,
        "vix": 18.87,
        "vvix": 98.15,
        "spy": 520.0,
        "vix_5d_roc": 1.2,
        "vvix_vix_ratio": 5.20,
        "spx_100d_ma": 510.0,
        "spx_distance_pct": 1.96,
        "cor1m": 0.42,
        "cor1m_previous_close": 0.40,
        "cor1m_5d_change": 0.05,
        "realized_vol": 14.5,
        "cri": {"score": 27.4, "level": "LOW", "components": {"vix_z": 0.3}},
        "cta": {
            "exposure_pct": 87.5,
            "forced_reduction_pct": 12.5,
            "forced_reduction": True,
            "selling_usd_b": 0.0,
        },
        "menthorq_cta": {"score": 5.1},
        "crash_trigger": {"triggered": False, "fired": False, "conditions": {}},
        "history": [],
        "spy_closes": [],
    }


def test_save_cri_scan_writes_row_with_derived_cri_level(engine):
    payload = _full_payload(date="2026-04-29")
    with engine.begin() as conn:
        save_cri_scan(conn, payload=payload)
        row = conn.execute(select(cri_series).where(cri_series.c.payload["date"].astext == "2026-04-29")).first()

    assert row is not None
    # cri_level is required (NOT NULL) and must be derived from payload['cri']['score']
    assert float(row.cri_level) == 27.4
    # alert defaults to false
    assert row.alert is False
    # generated columns confirm payload landed verbatim
    assert row.recorded_date.isoformat() == "2026-04-29"
    assert float(row.cri_score) == 27.4
    assert row.crash_trigger_fired is False
    assert row.cta_forced_reduction is True


def test_save_cri_scan_returns_id(engine):
    """Mirrors save_vcg_scan / save_gex_snapshot signature so the wider
    write path can capture the inserted id for outbox / event linkage."""
    payload = _full_payload(date="2026-04-30")
    with engine.begin() as conn:
        new_id = save_cri_scan(conn, payload=payload)
    assert isinstance(new_id, int)
    assert new_id > 0


def test_save_cri_scan_handles_missing_cri_score_field(engine):
    """The scanner emits payload['cri']['score'] but be defensive: an
    older payload shape without the score should land cri_level=0
    rather than crash, mirroring how cri_series test fixtures land
    Decimal('0') for empty payloads."""
    payload = {"date": "2026-05-01"}  # no cri.* keys at all
    with engine.begin() as conn:
        save_cri_scan(conn, payload=payload)
        row = conn.execute(select(cri_series).where(cri_series.c.payload["date"].astext == "2026-05-01")).first()
    assert row is not None
    assert float(row.cri_level) == 0.0


def test_save_cri_scan_appends_per_call(engine):
    """No ON CONFLICT — every call writes a new row. The latest-wins view
    in Phase 1 picks the most recent ORDER BY recorded_at; storage of
    intra-day ticks is intentional."""
    payload = _full_payload(date="2026-05-02")
    with engine.begin() as conn:
        save_cri_scan(conn, payload=payload)
        save_cri_scan(conn, payload=payload)
        rows = conn.execute(select(cri_series).where(cri_series.c.payload["date"].astext == "2026-05-02")).all()
    assert len(rows) == 2
