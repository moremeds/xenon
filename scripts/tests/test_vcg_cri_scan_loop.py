"""_vcg_cri_scan_loop runs scans, persists rows, and emits transition
events to events.outbox when (vcg_tier, cri_tier) changes.

UNKNOWN transitions are explicitly suppressed — a stale feed must NOT
trigger a regime_transition alert. Tested in test_unknown_suppression.

We exercise the inner `_vcg_cri_tick(last_seen)` function rather than
the outer `while True` loop so the test doesn't need to drive sleep
cancellation. The loop is just `tick → asyncio.sleep` repeated.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine

from scripts.tests.conftest import _sync_test_db_url
from xenon.db.schema import cri_series, outbox, vcg_series


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


def _cri_payload(*, score: float, vix: float, fired: bool = False):
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
            "forced_reduction": False,
            "forced_reduction_pct": 0.0,
            "selling_usd_b": 0.0,
        },
        "menthorq_cta": {"score": 0.0},
        "crash_trigger": {"triggered": fired, "fired": fired},
    }


def _seed(engine, *, vcg_tier: int | None, cri_score: float, fired: bool = False) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    with engine.begin() as conn:
        conn.execute(sa.delete(vcg_series))
        conn.execute(sa.delete(cri_series))
        conn.execute(
            sa.insert(vcg_series).values(
                scanned_at=now,
                payload=_vcg_payload(
                    tier=vcg_tier,
                    regime="ACTIVE" if vcg_tier else "DIVERGENCE",
                    vix=22.0,
                ),
            )
        )
        conn.execute(
            sa.insert(cri_series).values(
                recorded_at=now,
                cri_level=Decimal(str(cri_score)),
                payload=_cri_payload(score=cri_score, vix=22.0, fired=fired),
            )
        )


@pytest.fixture(autouse=True)
def _truncate_outbox(engine):
    with engine.begin() as conn:
        conn.execute(sa.delete(outbox))
    yield


@pytest.fixture(autouse=True)
def _init_async_engine():
    """The scripts/tests conftest only resets the sync engine; the loop
    tick uses the async engine via classify→_read_regime_row. Init it
    explicitly here and dispose at teardown."""
    import asyncio

    import xenon.db.engine as engine_mod

    engine_mod._engine = None  # type: ignore[attr-defined]
    engine_mod.init_engine()
    yield
    asyncio.get_event_loop().run_until_complete(engine_mod.dispose_engine())


async def _noop_scan() -> None:
    return None


@pytest.mark.asyncio
async def test_first_tick_seeds_last_seen_no_emit(engine, monkeypatch):
    """First tick after startup MUST NOT emit — there's no prior state to
    compare against. The loop seeds last_seen on this tick and emits
    only on subsequent transitions."""
    from xenon.api import server

    monkeypatch.setattr(server, "_run_vcg_scan_and_persist", _noop_scan)
    monkeypatch.setattr(server, "_run_cri_scan_and_persist", _noop_scan)

    _seed(engine, vcg_tier=None, cri_score=20.0)
    new_state = await server._vcg_cri_tick(last_seen=None)

    assert new_state == ("NORMAL", "NORMAL")
    with engine.connect() as c:
        rows = c.execute(sa.select(outbox).where(outbox.c.channel == "regime_transition")).all()
    assert rows == [], "first tick must seed last_seen, not emit"


@pytest.mark.asyncio
async def test_tier_transition_emits_outbox_row(engine, monkeypatch):
    from xenon.api import server

    monkeypatch.setattr(server, "_run_vcg_scan_and_persist", _noop_scan)
    monkeypatch.setattr(server, "_run_cri_scan_and_persist", _noop_scan)

    # NORMAL → TIER_2 transition (last_seen primed from a prior tick)
    _seed(engine, vcg_tier=2, cri_score=20.0)
    new_state = await server._vcg_cri_tick(last_seen=("NORMAL", "NORMAL"))

    assert new_state == ("TIER_2", "NORMAL")
    with engine.connect() as c:
        rows = c.execute(sa.select(outbox).where(outbox.c.channel == "regime_transition")).all()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["from"] == {"vcg": "NORMAL", "cri": "NORMAL"}
    assert payload["to"] == {"vcg": "TIER_2", "cri": "NORMAL"}
    assert payload["binding_tier"] == "TIER_2"
    assert payload["binding_side"] == "vcg"


@pytest.mark.asyncio
async def test_no_emit_when_tiers_unchanged(engine, monkeypatch):
    from xenon.api import server

    monkeypatch.setattr(server, "_run_vcg_scan_and_persist", _noop_scan)
    monkeypatch.setattr(server, "_run_cri_scan_and_persist", _noop_scan)

    _seed(engine, vcg_tier=2, cri_score=20.0)
    new_state = await server._vcg_cri_tick(last_seen=("TIER_2", "NORMAL"))

    assert new_state == ("TIER_2", "NORMAL")
    with engine.connect() as c:
        rows = c.execute(sa.select(outbox).where(outbox.c.channel == "regime_transition")).all()
    assert rows == []


@pytest.mark.asyncio
async def test_unknown_suppression_to_unknown(engine, monkeypatch):
    """TIER_2 → UNKNOWN (e.g. feed went stale) MUST NOT emit. The
    transition is real but we treat 'feed disappeared' as a degraded-state
    event, not a regime change."""
    from xenon.api import server

    async def _stale_read() -> dict:
        # Simulate what classify() sees when feeds are too old.
        return {"vcg_scanned_at": None, "cri_scanned_at": None}

    monkeypatch.setattr(server, "_run_vcg_scan_and_persist", _noop_scan)
    monkeypatch.setattr(server, "_run_cri_scan_and_persist", _noop_scan)
    monkeypatch.setattr("xenon.api.services.regime_state._read_regime_row", _stale_read)

    new_state = await server._vcg_cri_tick(last_seen=("TIER_2", "NORMAL"))

    assert new_state == ("UNKNOWN", "UNKNOWN")
    with engine.connect() as c:
        rows = c.execute(sa.select(outbox).where(outbox.c.channel == "regime_transition")).all()
    assert rows == [], "transitions involving UNKNOWN must not emit"


@pytest.mark.asyncio
async def test_unknown_suppression_from_unknown(engine, monkeypatch):
    """UNKNOWN → TIER_2 (feed came back online with a new tier) — also
    no emit. We require both endpoints of the transition to be concrete
    so the alert payload is meaningful."""
    from xenon.api import server

    monkeypatch.setattr(server, "_run_vcg_scan_and_persist", _noop_scan)
    monkeypatch.setattr(server, "_run_cri_scan_and_persist", _noop_scan)

    _seed(engine, vcg_tier=2, cri_score=20.0)
    new_state = await server._vcg_cri_tick(last_seen=("UNKNOWN", "NORMAL"))

    assert new_state == ("TIER_2", "NORMAL")
    with engine.connect() as c:
        rows = c.execute(sa.select(outbox).where(outbox.c.channel == "regime_transition")).all()
    assert rows == [], "transition with UNKNOWN endpoint must not emit"


@pytest.mark.asyncio
async def test_panic_transition_emits(engine, monkeypatch):
    """TIER_2 → PANIC must emit. PANIC is the regime change downstream
    consumers care about most."""
    from xenon.api import server

    monkeypatch.setattr(server, "_run_vcg_scan_and_persist", _noop_scan)
    monkeypatch.setattr(server, "_run_cri_scan_and_persist", _noop_scan)

    now = dt.datetime.now(dt.timezone.utc)
    with engine.begin() as conn:
        conn.execute(sa.delete(vcg_series))
        conn.execute(sa.delete(cri_series))
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

    new_state = await server._vcg_cri_tick(last_seen=("TIER_2", "NORMAL"))

    assert new_state == ("PANIC", "TIER_1")
    with engine.connect() as c:
        rows = c.execute(sa.select(outbox).where(outbox.c.channel == "regime_transition")).all()
    assert len(rows) == 1
    assert rows[0].payload["to"] == {"vcg": "PANIC", "cri": "TIER_1"}
    assert rows[0].payload["binding_tier"] == "PANIC"
