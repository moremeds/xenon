"""get_regime_state caches per (account_env, broker_account) for 30s.

XENON_REGIME_CACHE_TTL_S=0 disables caching entirely (used by tests that
want a fresh read on every call). Cache is in-process — no Redis, no
shared state across workers, by design (one FastAPI worker per host).
"""

from __future__ import annotations

import datetime as dt

import pytest

from xenon.api.services import regime_state as regime_state_module
from xenon.api.services.regime_state import (
    RegimeState,
    _cache_clear,
    get_regime_state,
)
from xenon.execution.account_scope import AccountScope


@pytest.fixture(autouse=True)
def clear_cache():
    _cache_clear()
    yield
    _cache_clear()


@pytest.fixture
def scope_a() -> AccountScope:
    return AccountScope(broker="IB", account_env="paper", broker_account="DU000")


@pytest.fixture
def scope_b() -> AccountScope:
    return AccountScope(broker="IB", account_env="paper", broker_account="DU999")


def _stub_row(now: dt.datetime) -> dict:
    return dict(
        vcg_scanned_at=now,
        vcg_tier_raw=None,
        vcg_regime="DIVERGENCE",
        vcg_ro=0,
        vcg_edr=0,
        vcg_bounce=0,
        vcg_sign_ok=True,
        vcg_pi_panic=0.0,
        vcg_vix=20.0,
        cri_scanned_at=now,
        cri_score=20.0,
        crash_trigger_fired=False,
        cta_forced_reduction=False,
        cri_vix=20.0,
    )


@pytest.mark.asyncio
async def test_first_call_reads_db_second_uses_cache(monkeypatch, scope_a):
    monkeypatch.setenv("XENON_REGIME_CACHE_TTL_S", "30")

    now = dt.datetime.now(dt.timezone.utc)
    call_count = {"n": 0}

    async def _stub_read():
        call_count["n"] += 1
        return _stub_row(now)

    monkeypatch.setattr(regime_state_module, "_read_regime_row", _stub_read)

    state_a = await get_regime_state(scope=scope_a)
    state_b = await get_regime_state(scope=scope_a)

    assert state_a is state_b, "cached read must return same dataclass instance"
    assert call_count["n"] == 1, "second call must not hit DB"
    assert isinstance(state_a, RegimeState)
    assert state_a.vcg_tier == "NORMAL"


@pytest.mark.asyncio
async def test_ttl_zero_disables_cache(monkeypatch, scope_a):
    monkeypatch.setenv("XENON_REGIME_CACHE_TTL_S", "0")
    now = dt.datetime.now(dt.timezone.utc)
    call_count = {"n": 0}

    async def _stub_read():
        call_count["n"] += 1
        return _stub_row(now)

    monkeypatch.setattr(regime_state_module, "_read_regime_row", _stub_read)

    state_a = await get_regime_state(scope=scope_a)
    state_b = await get_regime_state(scope=scope_a)

    assert state_a is not state_b
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_cache_keyed_by_scope(monkeypatch, scope_a, scope_b):
    """Different broker_accounts must NOT share a cache entry."""
    monkeypatch.setenv("XENON_REGIME_CACHE_TTL_S", "30")
    now = dt.datetime.now(dt.timezone.utc)
    call_count = {"n": 0}

    async def _stub_read():
        call_count["n"] += 1
        return _stub_row(now)

    monkeypatch.setattr(regime_state_module, "_read_regime_row", _stub_read)

    await get_regime_state(scope=scope_a)
    await get_regime_state(scope=scope_b)

    assert call_count["n"] == 2, "distinct scopes must each hit DB once"


@pytest.mark.asyncio
async def test_cache_expires_after_ttl(monkeypatch, scope_a):
    monkeypatch.setenv("XENON_REGIME_CACHE_TTL_S", "30")
    now = dt.datetime.now(dt.timezone.utc)
    call_count = {"n": 0}

    async def _stub_read():
        call_count["n"] += 1
        return _stub_row(now)

    monkeypatch.setattr(regime_state_module, "_read_regime_row", _stub_read)

    fake_clock = {"t": 1000.0}
    monkeypatch.setattr(regime_state_module.time, "monotonic", lambda: fake_clock["t"])

    await get_regime_state(scope=scope_a)
    assert call_count["n"] == 1

    # Within TTL — cache hit
    fake_clock["t"] = 1029.0
    await get_regime_state(scope=scope_a)
    assert call_count["n"] == 1

    # Past TTL — cache miss
    fake_clock["t"] = 1031.0
    await get_regime_state(scope=scope_a)
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_cold_start_returns_unknown_state(monkeypatch, scope_a):
    """When the view returns no rows (no scans yet), classify falls through
    to UNKNOWN/UNKNOWN → binding_tier=EDR (throttle, not block)."""
    monkeypatch.setenv("XENON_REGIME_CACHE_TTL_S", "0")

    async def _empty_read():
        return {"vcg_scanned_at": None, "cri_scanned_at": None}

    monkeypatch.setattr(regime_state_module, "_read_regime_row", _empty_read)

    state = await get_regime_state(scope=scope_a)
    assert state.vcg_tier == "UNKNOWN"
    assert state.cri_tier == "UNKNOWN"
    assert state.binding_tier == "EDR"
    assert state.is_stale is True
