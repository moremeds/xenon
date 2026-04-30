"""Regime state classifier and FastAPI dependency.

Reads from the `regime_state` PG view (Phase 1 migration) and projects raw
scanner outputs into the canonical six-tier ladder used by RegimeGate.

Tiers (most → least permissive):
    NORMAL  — both feeds calm
    EDR     — VCG early-deterioration regime (also where UNKNOWN is pegged)
    TIER_2  — VCG tier 2 OR CRI HIGH (50–74)
    TIER_1  — VCG tier 1 OR CRI CRITICAL (≥ 75 or crash trigger fired)
    PANIC   — VCG pi_panic ≥ 1.0
    UNKNOWN — feed missing or stale (>max_age_s old)

Classifier is a pure function — no DB, no HTTP, no clock dependency. Time
is injected explicitly so tests don't need monkeypatching.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from dataclasses import dataclass, field
from typing import Literal, Optional

import sqlalchemy as sa
from fastapi import Depends

TierLabel = Literal["NORMAL", "EDR", "TIER_2", "TIER_1", "PANIC", "UNKNOWN"]
BindingSide = Literal["vcg", "cri", "both", "none"]

# Ordinal ranking for binding_tier. UNKNOWN is pegged to EDR
# (throttle-not-block) so a missing feed sizes conservatively without
# halting the desk entirely.
_TIER_ORDINAL: dict[TierLabel, int] = {
    "NORMAL": 0,
    "EDR": 1,
    "UNKNOWN": 1,
    "TIER_2": 2,
    "TIER_1": 3,
    "PANIC": 4,
}


@dataclass(frozen=True)
class RegimeState:
    vcg_tier: TierLabel
    cri_tier: TierLabel
    binding_tier: TierLabel
    binding_side: BindingSide
    vcg_scanned_at: Optional[dt.datetime]
    cri_scanned_at: Optional[dt.datetime]
    is_stale: bool
    panic_active: bool
    raw: dict = field(default_factory=dict)


def _classify_vcg(row: dict, *, now: dt.datetime, max_age_s: int) -> TierLabel:
    scanned_at = row.get("vcg_scanned_at")
    if scanned_at is None:
        return "UNKNOWN"
    if (now - scanned_at).total_seconds() > max_age_s:
        return "UNKNOWN"
    if (row.get("vcg_pi_panic") or 0) >= 1.0:
        return "PANIC"
    tier = row.get("vcg_tier_raw")
    if tier == 1:
        return "TIER_1"
    if tier == 2:
        return "TIER_2"
    if (row.get("vcg_edr") or 0) == 1:
        return "EDR"
    return "NORMAL"


def _classify_cri(row: dict, *, now: dt.datetime, max_age_s: int) -> TierLabel:
    scanned_at = row.get("cri_scanned_at")
    if scanned_at is None:
        return "UNKNOWN"
    if (now - scanned_at).total_seconds() > max_age_s:
        return "UNKNOWN"
    score = row.get("cri_score") or 0
    if row.get("crash_trigger_fired") or score >= 75:
        return "TIER_1"
    if score >= 50:
        return "TIER_2"
    return "NORMAL"


def _binding(vcg_t: TierLabel, cri_t: TierLabel) -> tuple[TierLabel, BindingSide]:
    # Surface UNKNOWN as EDR for binding_tier — the gate consumes
    # binding_tier directly and UNKNOWN has no defined throttle row.
    def _surface(t: TierLabel) -> TierLabel:
        return "EDR" if t == "UNKNOWN" else t

    v_ord, c_ord = _TIER_ORDINAL[vcg_t], _TIER_ORDINAL[cri_t]
    if v_ord == 0 and c_ord == 0:
        return "NORMAL", "none"
    if v_ord > c_ord:
        return _surface(vcg_t), "vcg"
    if c_ord > v_ord:
        return _surface(cri_t), "cri"
    chosen = vcg_t if vcg_t != "UNKNOWN" else cri_t
    return _surface(chosen), "both"


def classify(row: dict, *, now: dt.datetime, max_age_s: int) -> RegimeState:
    """Project a regime_state view row into a RegimeState dataclass.

    `row` may be the empty dict (cold start) — this falls through to the
    UNKNOWN branch on both sides, which the gate treats as EDR throttle.
    """
    vcg_t = _classify_vcg(row, now=now, max_age_s=max_age_s)
    cri_t = _classify_cri(row, now=now, max_age_s=max_age_s)
    binding_tier, binding_side = _binding(vcg_t, cri_t)
    is_stale = "UNKNOWN" in (vcg_t, cri_t)
    panic_active = (row.get("vcg_vix") or 0) >= 48 or (row.get("cri_vix") or 0) >= 48
    return RegimeState(
        vcg_tier=vcg_t,
        cri_tier=cri_t,
        binding_tier=binding_tier,
        binding_side=binding_side,
        vcg_scanned_at=row.get("vcg_scanned_at"),
        cri_scanned_at=row.get("cri_scanned_at"),
        is_stale=is_stale,
        panic_active=panic_active,
        raw=dict(row),
    )


# ---- FastAPI dependency + per-scope TTL cache ---------------------------

_CacheKey = tuple[str, str]
_cache: dict[_CacheKey, tuple[float, RegimeState]] = {}


def _cache_clear() -> None:
    _cache.clear()


def _cache_get(key: _CacheKey, ttl_s: int) -> Optional[RegimeState]:
    entry = _cache.get(key)
    if entry is None:
        return None
    cached_at, state = entry
    if ttl_s == 0 or (time.monotonic() - cached_at) > ttl_s:
        return None
    return state


def _cache_set(key: _CacheKey, state: RegimeState) -> None:
    _cache[key] = (time.monotonic(), state)


async def _read_regime_row() -> dict:
    """Single-row SELECT against the regime_state view.

    Returns an empty dict when no rows exist (cold-start: either feed is
    empty). Classifier handles the empty case by emitting UNKNOWN tiers.
    """
    from xenon.db.engine import get_engine

    engine = get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(sa.text("SELECT * FROM xenon.regime_state"))
        row = result.mappings().first()
    if row is None:
        return {"vcg_scanned_at": None, "cri_scanned_at": None}
    return dict(row)


async def get_regime_state(
    scope=Depends(lambda: None),  # overridden below
) -> RegimeState:
    """FastAPI dep — current RegimeState, cached per (account_env, broker_account).

    Cache TTL via XENON_REGIME_CACHE_TTL_S (default 30s). Set to 0 to
    disable. Stale-feed cutoff via XENON_REGIME_MAX_AGE_S (default 90 min).
    """
    if scope is None:
        # Default resolution path — only triggered when called outside a
        # request context with an explicit scope.
        from xenon.api.guards import get_account_scope  # noqa: F401

        raise RuntimeError("get_regime_state requires an AccountScope (Depends or kwarg)")

    ttl_s = int(os.environ.get("XENON_REGIME_CACHE_TTL_S", "30"))
    max_age_s = int(os.environ.get("XENON_REGIME_MAX_AGE_S", str(90 * 60)))
    key = (scope.account_env, scope.broker_account)

    cached = _cache_get(key, ttl_s)
    if cached is not None:
        return cached

    row = await _read_regime_row()
    state = classify(row, now=dt.datetime.now(dt.timezone.utc), max_age_s=max_age_s)
    if ttl_s > 0:
        _cache_set(key, state)
    return state


# Wire FastAPI dependency injection — done at module import so the dep
# resolver picks up `get_account_scope` without leaking the import into
# the synchronous module body (test mode imports this without a fastapi
# app installed).
def _bind_default_scope_dep() -> None:
    from xenon.api.guards import get_account_scope

    get_regime_state.__defaults__ = (Depends(get_account_scope),)


_bind_default_scope_dep()
