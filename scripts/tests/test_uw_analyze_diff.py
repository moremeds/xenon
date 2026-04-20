"""Tests for the pure UW Analyze diff engine."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xenon.api.services.uw_analyze_diff import (  # noqa: E402
    IV_RANK_JUMP_PTS,
    MAX_PAIN_SHIFT_FRAC,
    UNUSUAL_PREMIUM_USD,
    Change,
    compute_changes,
)


def _snap(**derived):
    return {"derived": derived}


# ── No-diff cases ──────────────────────────────────────────────────────────


def test_no_prev_returns_empty():
    assert compute_changes(None, _snap(gex_sign="POSITIVE")) == []


def test_no_curr_returns_empty():
    assert compute_changes(_snap(gex_sign="POSITIVE"), None) == []


def test_both_none_returns_empty():
    assert compute_changes(None, None) == []


def test_identical_snapshots_no_changes():
    snap = _snap(gex_sign="POSITIVE", iv_rank=42, max_pain=100, spot=100, net_call_premium=1e6, net_put_premium=-1e6)
    assert compute_changes(snap, snap) == []


# ── GEX_FLIP_SIGN ──────────────────────────────────────────────────────────


def test_gex_flip_pos_to_neg_alerts():
    out = compute_changes(_snap(gex_sign="POSITIVE"), _snap(gex_sign="NEGATIVE"))
    assert len(out) == 1
    assert out[0].code == "GEX_FLIP_SIGN"
    assert out[0].severity == "alert"


def test_gex_flip_neg_to_pos_alerts():
    out = compute_changes(_snap(gex_sign="NEGATIVE"), _snap(gex_sign="POSITIVE"))
    assert any(c.code == "GEX_FLIP_SIGN" for c in out)


def test_gex_neutral_either_side_skipped():
    assert compute_changes(_snap(gex_sign="NEUTRAL"), _snap(gex_sign="POSITIVE")) == []
    assert compute_changes(_snap(gex_sign="POSITIVE"), _snap(gex_sign="NEUTRAL")) == []


def test_gex_null_skipped():
    assert compute_changes(_snap(gex_sign=None), _snap(gex_sign="POSITIVE")) == []
    assert compute_changes(_snap(gex_sign="POSITIVE"), _snap(gex_sign=None)) == []


# ── MAX_PAIN_SHIFT ─────────────────────────────────────────────────────────


def test_max_pain_at_threshold_fires():
    # spot=100, threshold 2% → delta of 2.0 fires
    out = compute_changes(
        _snap(max_pain=100.0, spot=100.0),
        _snap(max_pain=102.0, spot=100.0),
    )
    assert any(c.code == "MAX_PAIN_SHIFT" for c in out)


def test_max_pain_below_threshold_silent():
    out = compute_changes(
        _snap(max_pain=100.0, spot=100.0),
        _snap(max_pain=101.9, spot=100.0),
    )
    assert all(c.code != "MAX_PAIN_SHIFT" for c in out)


def test_max_pain_null_skipped():
    out = compute_changes(_snap(max_pain=None, spot=100.0), _snap(max_pain=110.0, spot=100.0))
    assert all(c.code != "MAX_PAIN_SHIFT" for c in out)
    out = compute_changes(_snap(max_pain=100.0, spot=100.0), _snap(max_pain=None, spot=100.0))
    assert all(c.code != "MAX_PAIN_SHIFT" for c in out)


def test_max_pain_zero_spot_skipped():
    """Zero-guard: should not raise ZeroDivisionError."""
    out = compute_changes(
        _snap(max_pain=100.0, spot=0),
        _snap(max_pain=110.0, spot=0),
    )
    assert all(c.code != "MAX_PAIN_SHIFT" for c in out)


def test_max_pain_null_spot_skipped():
    out = compute_changes(
        _snap(max_pain=100.0, spot=None),
        _snap(max_pain=110.0, spot=None),
    )
    assert all(c.code != "MAX_PAIN_SHIFT" for c in out)


# ── IV_RANK_JUMP ───────────────────────────────────────────────────────────


def test_iv_rank_jump_at_threshold_fires():
    out = compute_changes(_snap(iv_rank=20), _snap(iv_rank=30))
    assert any(c.code == "IV_RANK_JUMP" for c in out)


def test_iv_rank_jump_below_threshold_silent():
    out = compute_changes(_snap(iv_rank=20), _snap(iv_rank=29.9))
    assert all(c.code != "IV_RANK_JUMP" for c in out)


def test_iv_rank_drop_at_threshold_fires():
    out = compute_changes(_snap(iv_rank=50), _snap(iv_rank=40))
    assert any(c.code == "IV_RANK_JUMP" for c in out)


def test_iv_rank_null_skipped():
    out = compute_changes(_snap(iv_rank=None), _snap(iv_rank=50))
    assert all(c.code != "IV_RANK_JUMP" for c in out)


# ── UNUSUAL_CALL_SWEEP ─────────────────────────────────────────────────────


def test_call_sweep_at_threshold_fires():
    out = compute_changes(
        _snap(net_call_premium=0),
        _snap(net_call_premium=UNUSUAL_PREMIUM_USD),
    )
    assert any(c.code == "UNUSUAL_CALL_SWEEP" for c in out)


def test_call_sweep_below_threshold_silent():
    out = compute_changes(
        _snap(net_call_premium=0),
        _snap(net_call_premium=UNUSUAL_PREMIUM_USD - 1),
    )
    assert all(c.code != "UNUSUAL_CALL_SWEEP" for c in out)


def test_call_sweep_negative_delta_silent():
    """A drop in net_call_premium is not a call sweep."""
    out = compute_changes(
        _snap(net_call_premium=10e6),
        _snap(net_call_premium=0),
    )
    assert all(c.code != "UNUSUAL_CALL_SWEEP" for c in out)


def test_call_sweep_null_skipped():
    from xenon.api.services.uw_analyze_diff import compute_changes

    prev = {"derived": {"net_call_premium": None}}
    curr = {"derived": {"net_call_premium": 10_000_000}}
    out = compute_changes(prev, curr)
    assert all(c.code != "UNUSUAL_CALL_SWEEP" for c in out)


def test_call_sweep_prev_null_skipped():
    from xenon.api.services.uw_analyze_diff import compute_changes

    prev = {"derived": {"net_call_premium": 0}}
    curr = {"derived": {"net_call_premium": None}}
    assert all(c.code != "UNUSUAL_CALL_SWEEP" for c in compute_changes(prev, curr))


def test_max_pain_zero_spot_skipped():
    from xenon.api.services.uw_analyze_diff import compute_changes

    prev = {"derived": {"max_pain": 100}}
    curr = {"derived": {"max_pain": 110, "spot": 0}}
    assert all(c.code != "MAX_PAIN_SHIFT" for c in compute_changes(prev, curr))


# ── UNUSUAL_PUT_SWEEP ──────────────────────────────────────────────────────


def test_put_sweep_at_threshold_fires():
    out = compute_changes(
        _snap(net_put_premium=0),
        _snap(net_put_premium=-UNUSUAL_PREMIUM_USD),
    )
    assert any(c.code == "UNUSUAL_PUT_SWEEP" for c in out)


def test_put_sweep_above_threshold_silent():
    """A rise in net_put_premium is not a put sweep."""
    out = compute_changes(
        _snap(net_put_premium=-10e6),
        _snap(net_put_premium=0),
    )
    assert all(c.code != "UNUSUAL_PUT_SWEEP" for c in out)


def test_put_sweep_null_skipped():
    out = compute_changes(_snap(net_put_premium=None), _snap(net_put_premium=-10e6))
    assert all(c.code != "UNUSUAL_PUT_SWEEP" for c in out)


# ── Combined ───────────────────────────────────────────────────────────────


def test_multiple_rules_can_fire_together():
    prev = _snap(
        gex_sign="POSITIVE",
        iv_rank=20,
        max_pain=100,
        spot=100,
        net_call_premium=0,
        net_put_premium=0,
    )
    curr = _snap(
        gex_sign="NEGATIVE",
        iv_rank=40,
        max_pain=110,
        spot=100,
        net_call_premium=10e6,
        net_put_premium=-10e6,
    )
    codes = {c.code for c in compute_changes(prev, curr)}
    assert "GEX_FLIP_SIGN" in codes
    assert "IV_RANK_JUMP" in codes
    assert "MAX_PAIN_SHIFT" in codes
    assert "UNUSUAL_CALL_SWEEP" in codes
    assert "UNUSUAL_PUT_SWEEP" in codes


def test_change_to_dict_round_trips():
    c = Change(code="IV_RANK_JUMP", label="x", prev=10, curr=25, severity="warn")
    d = c.to_dict()
    assert d == {"code": "IV_RANK_JUMP", "label": "x", "prev": 10, "curr": 25, "severity": "warn"}
