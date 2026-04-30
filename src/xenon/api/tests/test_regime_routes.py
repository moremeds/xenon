"""GET /regime + GET /regime/overrides — read-only regime surface.

These routes expose the same RegimeState computed by `get_regime_state`
(the FastAPI dep that the order route consumes in Phase 3). Keeping
read and write on the same dep means any classifier change is visible
to the UI without a separate code path.
"""

from __future__ import annotations

import datetime as dt
import secrets

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from xenon.api.server import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_regime_cache():
    from xenon.api.services.regime_state import _cache_clear

    _cache_clear()
    yield
    _cache_clear()


def _seed_vcg_cri(scanned_at: dt.datetime, *, vcg_tier: int | None, cri_score: float) -> None:
    from decimal import Decimal

    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import cri_series, vcg_series

    engine = get_sync_engine()
    with engine.begin() as conn:
        conn.execute(
            sa.insert(vcg_series).values(
                scanned_at=scanned_at,
                payload={
                    "signal": {
                        "vcg": 2.5,
                        "vcg_adj": 2.5,
                        "tier": vcg_tier,
                        "regime": "ACTIVE" if vcg_tier else "DIVERGENCE",
                        "ro": 0,
                        "edr": 0,
                        "bounce": 0,
                        "sign_ok": True,
                        "sign_suppressed": False,
                        "pi_panic": 0.0,
                        "vix": 22.0,
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
                },
            )
        )
        conn.execute(
            sa.insert(cri_series).values(
                recorded_at=scanned_at,
                cri_level=Decimal(str(cri_score)),
                payload={
                    "date": "2026-04-29",
                    "vix": 22.0,
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
                    "cri": {"score": cri_score, "components": {}},
                    "cta": {
                        "exposure_pct": 70.0,
                        "forced_reduction": False,
                        "forced_reduction_pct": 0.0,
                        "selling_usd_b": 0.0,
                    },
                    "menthorq_cta": {"score": 0.0},
                    "crash_trigger": {"triggered": False, "fired": False},
                },
            )
        )


def test_get_regime_returns_payload_shape(client):
    _seed_vcg_cri(
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5),
        vcg_tier=2,
        cri_score=20.0,
    )
    res = client.get("/regime")
    assert res.status_code == 200
    body = res.json()
    for k in (
        "vcg_tier",
        "cri_tier",
        "binding_tier",
        "binding_side",
        "vcg_scanned_at",
        "cri_scanned_at",
        "is_stale",
        "panic_active",
    ):
        assert k in body, f"missing key {k}"
    assert "raw" not in body, "internal raw dict must not leak"


def test_get_regime_classifies_tier_2(client):
    _seed_vcg_cri(
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5),
        vcg_tier=2,
        cri_score=20.0,
    )
    res = client.get("/regime")
    body = res.json()
    assert body["vcg_tier"] == "TIER_2"
    assert body["binding_side"] == "vcg"


def test_get_regime_cold_start_returns_unknown(client):
    """No scans yet → both feeds UNKNOWN, binding throttle = EDR."""
    res = client.get("/regime")
    assert res.status_code == 200
    body = res.json()
    assert body["vcg_tier"] == "UNKNOWN"
    assert body["cri_tier"] == "UNKNOWN"
    assert body["binding_tier"] == "EDR"
    assert body["is_stale"] is True


def test_get_regime_sets_cache_control_header(client):
    _seed_vcg_cri(dt.datetime.now(dt.timezone.utc), vcg_tier=None, cri_score=20.0)
    res = client.get("/regime")
    assert res.headers.get("Cache-Control", "").startswith("private")


def test_get_regime_overrides_empty_returns_paged_envelope(client):
    res = client.get("/regime/overrides")
    assert res.status_code == 200
    body = res.json()
    assert "items" in body and isinstance(body["items"], list)
    assert "limit" in body
    assert body["items"] == []


def test_get_regime_overrides_paginates_and_filters_by_scope(client):
    """Inserts must be scoped: rows from a different broker_account
    must not leak into this scope's listing."""
    from xenon.db.engine import get_sync_engine
    from xenon.db.schema import order_submissions, regime_overrides

    engine = get_sync_engine()
    now = dt.datetime.now(dt.timezone.utc)

    with engine.begin() as conn:
        # In-scope row (broker_account=DU0000000 matches the test default)
        sub_a = f"SUB-A-{secrets.token_hex(4)}"
        conn.execute(
            sa.insert(order_submissions).values(
                submission_id=sub_a,
                user_id="u1",
                ticker="AAPL",
                security_type="STK",
                action="BUY",
                quantity=1,
                state="RESERVED",
                submitted_at=now,
                broker="IB",
                account_env="paper",
                broker_account="DU0000000",
            )
        )
        conn.execute(
            sa.insert(regime_overrides).values(
                user_id="u1",
                account_env="paper",
                broker="ib",
                broker_account="DU0000000",
                submission_id=sub_a,
                route="POST /orders/place",
                binding_side="cri",
                block_reason="CRI CRITICAL",
                user_reason="hedge anyway",
                order_payload={"symbol": "AAPL"},
            )
        )

        # Out-of-scope row (different broker_account)
        sub_b = f"SUB-B-{secrets.token_hex(4)}"
        conn.execute(
            sa.insert(order_submissions).values(
                submission_id=sub_b,
                user_id="u1",
                ticker="AAPL",
                security_type="STK",
                action="BUY",
                quantity=1,
                state="RESERVED",
                submitted_at=now,
                broker="IB",
                account_env="paper",
                broker_account="DU9999999",
            )
        )
        conn.execute(
            sa.insert(regime_overrides).values(
                user_id="u1",
                account_env="paper",
                broker="ib",
                broker_account="DU9999999",
                submission_id=sub_b,
                route="POST /orders/place",
                binding_side="vcg",
                block_reason="VCG TIER_1",
                user_reason="other-account override",
                order_payload={"symbol": "AAPL"},
            )
        )

    res = client.get("/regime/overrides?limit=10")
    assert res.status_code == 200
    body = res.json()
    submissions = {item["submission_id"] for item in body["items"]}
    assert sub_a in submissions
    assert sub_b not in submissions, "must not surface other-scope override"


def test_get_regime_overrides_clamps_limit(client):
    res = client.get("/regime/overrides?limit=999")
    assert res.status_code == 422  # Query(le=200) rejects oversize limit
