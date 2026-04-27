"""Backfill vcg_series from data/vcg.json."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import vcg_series


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def test_backfill_inserts_current_plus_history(engine, tmp_path):
    src_data = {
        "scan_time": "2026-04-21T14:11:08.383805",
        "market_open": False,
        "credit_proxy": "HYG",
        "signal": {
            "vcg": 1.0416,
            "vcg_adj": 1.0416,
            "residual": 0.002108,
            "beta1_vvix": -0.06,
            "beta2_vix": -0.01,
            "alpha": -0.0001,
            "vix": 18.87,
            "vvix": 98.15,
            "credit_price": 80.58,
            "credit_5d_return_pct": 0.4,
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
                "vvix_component": -0.0019,
                "vix_component": -0.0009,
                "model_implied": -0.0029,
            },
        },
        "history": [
            {
                "date": "2026-03-23",
                "residual": 0.006,
                "vcg": 3.11,
                "vcg_adj": 3.11,
                "beta1": -0.013,
                "beta2": -0.023,
                "vix": 26.15,
                "vvix": 122.82,
                "credit": 79.44,
                "ro": 0,
                "edr": 1,
                "tier": 3,
                "bounce": 0,
            },
            {
                "date": "2026-03-24",
                "residual": -0.001,
                "vcg": -0.93,
                "vcg_adj": -0.93,
                "beta1": -0.011,
                "beta2": -0.025,
                "vix": 26.95,
                "vvix": 124.14,
                "credit": 79.17,
                "ro": 0,
                "edr": 0,
                "tier": None,
                "bounce": 0,
            },
        ],
    }
    src = tmp_path / "vcg.json"
    src.write_text(json.dumps(src_data))

    from scripts.migrations import _2026_04_26_backfill_vcg_history as bf

    n = bf.run(json_path=src, db_url=_sync_test_db_url())
    assert n == 3  # 2 history + 1 current

    with engine.begin() as conn:
        rows = conn.execute(select(vcg_series).order_by(vcg_series.c.scanned_at)).all()
    assert len(rows) == 3
    assert float(rows[0].vcg) == 3.11
    assert rows[0].tier == 3
    current_row = next(r for r in rows if r.regime == "DIVERGENCE")
    assert float(current_row.attr_model_implied) == -0.0029


def test_backfill_idempotent(engine, tmp_path):
    src_data = {
        "scan_time": "2026-04-21T14:11:08.383805",
        "market_open": False,
        "credit_proxy": "HYG",
        "signal": {"vcg": 1.0, "regime": "X"},
        "history": [
            {"date": "2026-03-23", "vcg": 3.11},
        ],
    }
    src = tmp_path / "vcg.json"
    src.write_text(json.dumps(src_data))
    from scripts.migrations import _2026_04_26_backfill_vcg_history as bf

    bf.run(json_path=src, db_url=_sync_test_db_url())
    n2 = bf.run(json_path=src, db_url=_sync_test_db_url())
    assert n2 == 0  # second run no-op

    with engine.begin() as conn:
        rows = conn.execute(select(vcg_series)).all()
    assert len(rows) == 2  # not 4
