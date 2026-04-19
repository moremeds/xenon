"""Tests for the OI tracker notability gates."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from xenon.api.services.uw_analyze_oi_tracker import (  # noqa: E402
    NEAR_SPOT_PCT,
    NOTABLE_ABSOLUTE,
    NOTABLE_PCT,
    diff_oi,
)


def _row(strike, *, prev_call=0, curr_call=0, prev_put=0, curr_put=0):
    return {
        "strike": strike,
        "prev_call_oi": prev_call,
        "call_oi": curr_call,
        "prev_put_oi": prev_put,
        "put_oi": curr_put,
    }


def test_returns_empty_when_spot_missing():
    out = diff_oi([_row(100, prev_call=100, curr_call=10000)], spot=None)
    assert out == []


def test_returns_empty_when_spot_zero():
    out = diff_oi([_row(100, prev_call=100, curr_call=10000)], spot=0)
    assert out == []


def test_call_oi_at_threshold_fires():
    # +1000 contracts and +25% relative.
    out = diff_oi(
        [_row(100, prev_call=4000, curr_call=5000)],
        spot=100,
    )
    assert len(out) == 1
    c = out[0]
    assert c.side == "call"
    assert c.delta == 1000
    assert c.delta_pct == 0.25


def test_below_absolute_threshold_silent():
    out = diff_oi(
        [_row(100, prev_call=4000, curr_call=4999)],
        spot=100,
    )
    assert out == []


def test_below_pct_threshold_silent():
    # Big absolute, but tiny percentage.
    out = diff_oi(
        [_row(100, prev_call=100_000, curr_call=101_500)],
        spot=100,
    )
    assert out == []


def test_strike_far_from_spot_skipped():
    # Strike +6% from spot — beyond ±5% gate.
    out = diff_oi(
        [_row(106, prev_call=4000, curr_call=10_000)],
        spot=100,
    )
    assert out == []


def test_prev_zero_uses_absolute_only():
    # New strike: prev=0, but absolute add ≥ 1000 → fires.
    out = diff_oi(
        [_row(100, prev_call=0, curr_call=1500)],
        spot=100,
    )
    assert len(out) == 1
    assert out[0].delta_pct == 0.0


def test_prev_zero_below_absolute_silent():
    out = diff_oi(
        [_row(100, prev_call=0, curr_call=999)],
        spot=100,
    )
    assert out == []


def test_put_side_separately_evaluated():
    out = diff_oi(
        [_row(100, prev_put=2000, curr_put=4000)],
        spot=100,
    )
    assert len(out) == 1
    assert out[0].side == "put"


def test_both_sides_can_fire_on_same_strike():
    out = diff_oi(
        [_row(100, prev_call=4000, curr_call=10_000, prev_put=2000, curr_put=5000)],
        spot=100,
    )
    sides = {c.side for c in out}
    assert sides == {"call", "put"}


def test_negative_delta_fires_when_large():
    """OI evaporation: 10K → 1K with absolute drop ≥1000 and pct ≥25%."""
    out = diff_oi(
        [_row(100, prev_call=10_000, curr_call=1_000)],
        spot=100,
    )
    assert len(out) == 1
    assert out[0].delta == -9000
    assert out[0].delta_pct < 0


def test_results_sorted_by_abs_delta_desc():
    out = diff_oi(
        [
            _row(100, prev_call=4000, curr_call=5000),  # +1000
            _row(101, prev_call=4000, curr_call=9000),  # +5000
            _row(99, prev_call=4000, curr_call=6500),  # +2500
        ],
        spot=100,
    )
    deltas = [c.delta for c in out]
    assert deltas == sorted(deltas, key=abs, reverse=True)


def test_label_format():
    out = diff_oi(
        [_row(100, prev_call=4000, curr_call=10_000)],
        spot=100,
    )
    assert out[0].side == "call"
    assert "calls" in out[0].label
    assert "@ $100" in out[0].label
