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
from dataclasses import dataclass, field
from typing import Literal, Optional

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
