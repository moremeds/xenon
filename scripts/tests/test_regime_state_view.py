"""regime_state view shape — latest VCG x latest CRI cross join.

Returns zero rows when either underlying table is empty (CROSS JOIN
semantics with LIMIT 1 CTEs); returns a single row with the most recent
scan from each side when both have data.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, text

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import cri_series, vcg_series


@pytest.fixture
def engine():
    eng = create_engine(_sync_test_db_url(), pool_pre_ping=True)
    yield eng
    eng.dispose()


def _vcg_payload(*, tier: int | None, regime: str, vix: float, edr: int = 0, pi_panic: float = 0.0):
    return {
        "signal": {
            "vcg": 2.5,
            "vcg_adj": 2.5,
            "tier": tier,
            "regime": regime,
            "ro": 0,
            "edr": edr,
            "bounce": 0,
            "sign_ok": True,
            "sign_suppressed": False,
            "pi_panic": pi_panic,
            "vix": vix,
            "vvix": 100.0,
            "credit_price": 5.0,
            "credit_5d_return_pct": 0.0,
            "residual": 0.0,
            "beta1_vvix": 0.0,
            "beta2_vix": 0.0,
            "alpha": 0.0,
            "vvix_severity": "NORMAL",
            "interpretation": "test",
            "attribution": {
                "vvix_pct": 50.0,
                "vix_pct": 50.0,
                "vvix_component": 0.0,
                "vix_component": 0.0,
                "model_implied": 0.0,
            },
        }
    }


def _cri_payload(*, score: float, vix: float, fired: bool = False, forced: bool = False):
    return {
        "date": "2026-04-29",
        "vix": vix,
        "vvix": 100.0,
        "spy": 510.0,
        "vix_5d_roc": 0.0,
        "vvix_vix_ratio": 4.5,
        "spx_100d_ma": 505.0,
        "spx_distance_pct": 1.0,
        "cor1m": 0.4,
        "cor1m_previous_close": 0.4,
        "cor1m_5d_change": 0.0,
        "realized_vol": 18.0,
        "cri": {"score": score, "components": {}},
        "cta": {
            "exposure_pct": 70.0,
            "forced_reduction": forced,
            "forced_reduction_pct": 0.0,
            "selling_usd_b": 0.0,
        },
        "menthorq_cta": {"score": 0.0},
        "crash_trigger": {"triggered": fired, "fired": fired},
    }


def test_view_zero_rows_when_vcg_empty(engine):
    with engine.begin() as conn:
        # cri populated, vcg empty
        conn.execute(
            sa.insert(cri_series).values(
                recorded_at=dt.datetime(2026, 4, 29, 14, 0, tzinfo=dt.timezone.utc),
                cri_level=Decimal("20.0"),
                payload=_cri_payload(score=20.0, vix=20.0),
            )
        )
        rows = conn.execute(text("SELECT * FROM xenon.regime_state")).all()
    assert rows == []


def test_view_zero_rows_when_cri_empty(engine):
    with engine.begin() as conn:
        # vcg populated, cri empty
        conn.execute(
            sa.insert(vcg_series).values(
                scanned_at=dt.datetime(2026, 4, 29, 14, 0, tzinfo=dt.timezone.utc),
                payload=_vcg_payload(tier=2, regime="ACTIVE", vix=29.0),
            )
        )
        rows = conn.execute(text("SELECT * FROM xenon.regime_state")).all()
    assert rows == []


def test_view_returns_latest_of_each(engine):
    older = dt.datetime(2026, 4, 29, 12, 0, tzinfo=dt.timezone.utc)
    newer = dt.datetime(2026, 4, 29, 15, 0, tzinfo=dt.timezone.utc)

    with engine.begin() as conn:
        # Two VCG rows (newer should win)
        conn.execute(
            sa.insert(vcg_series).values(
                scanned_at=older,
                payload=_vcg_payload(tier=3, regime="WATCH", vix=20.0),
            )
        )
        conn.execute(
            sa.insert(vcg_series).values(
                scanned_at=newer,
                payload=_vcg_payload(tier=2, regime="ACTIVE", vix=29.0),
            )
        )
        # Two CRI rows (newer should win)
        conn.execute(
            sa.insert(cri_series).values(
                recorded_at=older,
                cri_level=Decimal("30.0"),
                payload=_cri_payload(score=30.0, vix=20.0),
            )
        )
        conn.execute(
            sa.insert(cri_series).values(
                recorded_at=newer,
                cri_level=Decimal("42.0"),
                payload=_cri_payload(score=42.0, vix=29.0, fired=False),
            )
        )

        row = conn.execute(
            text(
                """SELECT vcg_tier_raw, vcg_regime, cri_score,
                          crash_trigger_fired, cta_forced_reduction
                   FROM xenon.regime_state"""
            )
        ).one()

    assert row.vcg_tier_raw == 2
    assert row.vcg_regime == "ACTIVE"
    assert float(row.cri_score) == 42.0
    assert row.crash_trigger_fired is False
    assert row.cta_forced_reduction is False


def test_view_projects_panic_signal(engine):
    """pi_panic and crash_trigger_fired propagate so the Python classifier
    can map them to the PANIC/TIER_1 tier."""
    now = dt.datetime(2026, 4, 29, 15, 0, tzinfo=dt.timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            sa.insert(vcg_series).values(
                scanned_at=now,
                payload=_vcg_payload(tier=1, regime="PANIC", vix=49.0, pi_panic=1.0),
            )
        )
        conn.execute(
            sa.insert(cri_series).values(
                recorded_at=now,
                cri_level=Decimal("80.0"),
                payload=_cri_payload(score=80.0, vix=49.0, fired=True),
            )
        )
        row = conn.execute(
            text(
                """SELECT vcg_pi_panic, vcg_vix, cri_vix,
                          cri_score, crash_trigger_fired
                   FROM xenon.regime_state"""
            )
        ).one()
    assert float(row.vcg_pi_panic) == 1.0
    assert float(row.vcg_vix) == 49.0
    assert float(row.cri_vix) == 49.0
    assert float(row.cri_score) == 80.0
    assert row.crash_trigger_fired is True
