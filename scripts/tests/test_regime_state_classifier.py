"""Pure-function classifier tests — no DB, no HTTP.

The classifier projects raw `regime_state` view rows into the canonical
six-tier ladder used by RegimeGate. Stale (>max_age_s) or missing
scanned_at falls through to UNKNOWN; UNKNOWN is pegged at EDR ordinal so
it throttles rather than blocks.
"""

from __future__ import annotations

import datetime as dt

import pytest
from xenon.api.services.regime_state import RegimeState, classify

_NOW = dt.datetime(2026, 4, 29, 15, 0, tzinfo=dt.timezone.utc)
_FRESH = _NOW - dt.timedelta(minutes=10)
_STALE = _NOW - dt.timedelta(hours=2)


def _row(**kw) -> dict:
    base = dict(
        vcg_scanned_at=_FRESH,
        vcg_tier_raw=None,
        vcg_regime="DIVERGENCE",
        vcg_ro=0,
        vcg_edr=0,
        vcg_bounce=0,
        vcg_sign_ok=True,
        vcg_pi_panic=0.0,
        vcg_vix=20.0,
        cri_scanned_at=_FRESH,
        cri_score=20.0,
        crash_trigger_fired=False,
        cta_forced_reduction=False,
        cri_vix=20.0,
    )
    base.update(kw)
    return base


@pytest.mark.parametrize(
    "row,expected",
    [
        (_row(), ("NORMAL", "NORMAL", "NORMAL", "none")),
        (_row(vcg_edr=1, vcg_regime="WATCH"), ("EDR", "NORMAL", "EDR", "vcg")),
        (_row(vcg_tier_raw=2, vcg_regime="ACTIVE"), ("TIER_2", "NORMAL", "TIER_2", "vcg")),
        (_row(vcg_tier_raw=1, vcg_regime="ACTIVE"), ("TIER_1", "NORMAL", "TIER_1", "vcg")),
        (_row(vcg_pi_panic=1.0, vcg_vix=49.0), ("PANIC", "NORMAL", "PANIC", "vcg")),
        (_row(cri_score=60.0), ("NORMAL", "TIER_2", "TIER_2", "cri")),
        (_row(cri_score=80.0), ("NORMAL", "TIER_1", "TIER_1", "cri")),
        (_row(crash_trigger_fired=True), ("NORMAL", "TIER_1", "TIER_1", "cri")),
        (_row(vcg_tier_raw=2, cri_score=60.0), ("TIER_2", "TIER_2", "TIER_2", "both")),
        (_row(vcg_tier_raw=1, cri_score=60.0), ("TIER_1", "TIER_2", "TIER_1", "vcg")),
        (_row(vcg_scanned_at=_STALE), ("UNKNOWN", "NORMAL", "EDR", "vcg")),
        (
            _row(vcg_scanned_at=_STALE, cri_scanned_at=_STALE),
            ("UNKNOWN", "UNKNOWN", "EDR", "both"),
        ),
        (_row(vcg_scanned_at=None, cri_scanned_at=None), ("UNKNOWN", "UNKNOWN", "EDR", "both")),
    ],
)
def test_classifier_table(row, expected):
    state = classify(row, now=_NOW, max_age_s=90 * 60)
    got = (state.vcg_tier, state.cri_tier, state.binding_tier, state.binding_side)
    assert got == expected


def test_panic_active_flag_set_on_high_vix():
    state = classify(_row(vcg_vix=49.5), now=_NOW, max_age_s=90 * 60)
    assert state.panic_active is True


def test_panic_active_flag_clear_when_both_below_threshold():
    state = classify(_row(vcg_vix=30.0, cri_vix=30.0), now=_NOW, max_age_s=90 * 60)
    assert state.panic_active is False


def test_returned_state_is_frozen_dataclass():
    state = classify(_row(), now=_NOW, max_age_s=90 * 60)
    assert isinstance(state, RegimeState)
    with pytest.raises(Exception):
        state.vcg_tier = "PANIC"  # type: ignore[misc]
