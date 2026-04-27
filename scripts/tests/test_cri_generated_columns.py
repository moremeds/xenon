"""CRI generated columns derive from payload."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, insert, select

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import cri_series


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def test_cri_payload_extracts_to_columns(engine):
    payload = {
        "date": "2026-04-08",
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
        "cri": {"score": 12.3, "components": {"vix_z": 0.3, "spy_z": -0.1}},
        "cta": {"exposure_pct": 87.5, "forced_reduction": False, "selling_usd_b": 0.0},
        "menthorq_cta": {"score": 5.1},
        "crash_trigger": {"fired": False, "conditions": {}},
        "history": [],
        "spy_closes": [],
    }
    with engine.begin() as conn:
        conn.execute(
            insert(cri_series).values(
                cri_level=Decimal("12.3"),
                alert=False,
                payload=payload,
            )
        )
        row = conn.execute(select(cri_series)).first()
    assert row.recorded_date.isoformat() == "2026-04-08"
    assert float(row.vix) == 18.87
    assert float(row.vvix) == 98.15
    assert float(row.cri_score) == 12.3
    assert row.cri_components == {"vix_z": 0.3, "spy_z": -0.1}
    assert float(row.cta_exposure_pct) == 87.5
    assert row.cta_forced_reduction is False
    assert float(row.menthorq_cta_score) == 5.1
    assert row.crash_trigger_fired is False


def test_cri_partial_payload_gives_nulls(engine):
    payload = {"date": "2026-04-09", "vix": 20.0}
    with engine.begin() as conn:
        conn.execute(
            insert(cri_series).values(
                cri_level=Decimal("0"),
                alert=False,
                payload=payload,
            )
        )
        row = conn.execute(select(cri_series)).first()
    assert float(row.vix) == 20.0
    assert row.vvix is None
    assert row.cri_score is None
    assert row.crash_trigger_fired is None
