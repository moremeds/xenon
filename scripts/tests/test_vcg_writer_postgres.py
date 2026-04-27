"""VCG scanner persists to vcg_series with generated columns."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.queries.scans import save_vcg_scan
from xenon.db.schema import vcg_series


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


SAMPLE_VCG = {
    "scan_time": "2026-04-21T14:11:08.383805",
    "market_open": False,
    "credit_proxy": "HYG",
    "signal": {
        "vcg": 1.0416,
        "vcg_adj": 1.0416,
        "residual": 0.002108,
        "beta1_vvix": -0.061933,
        "beta2_vix": -0.011826,
        "alpha": -0.000135,
        "vix": 18.87,
        "vvix": 98.15,
        "credit_price": 80.58,
        "credit_5d_return_pct": 0.399,
        "ro": 0,
        "edr": 0,
        "tier": None,
        "bounce": 0,
        "vvix_severity": "moderate",
        "sign_ok": True,
        "sign_suppressed": False,
        "pi_panic": 0.0,
        "regime": "DIVERGENCE",
        "interpretation": "NORMAL",
        "attribution": {
            "vvix_pct": 68.1,
            "vix_pct": 31.9,
            "vvix_component": -0.001936,
            "vix_component": -0.000905,
            "model_implied": -0.002976,
        },
    },
    "history": [],
}


def test_save_vcg_scan_extracts_signal(engine):
    with engine.begin() as conn:
        new_id = save_vcg_scan(conn, payload=SAMPLE_VCG, market_open=False, credit_proxy="HYG")
        row = conn.execute(select(vcg_series).where(vcg_series.c.id == new_id)).first()
    assert row.market_open is False
    assert row.credit_proxy == "HYG"
    assert float(row.vcg) == 1.0416
    assert float(row.vix) == 18.87
    assert float(row.vvix) == 98.15
    assert row.regime == "DIVERGENCE"
    assert row.interpretation == "NORMAL"
    assert row.tier is None
    assert row.sign_ok is True
    assert float(row.attr_vvix_pct) == 68.1
    assert float(row.attr_model_implied) == -0.002976
