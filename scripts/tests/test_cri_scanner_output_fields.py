"""CRI scanner emits the boolean fields the cri_series schema generated
columns expect, alongside the existing numeric/legacy fields.

Schema reference (src/xenon/db/schema.py):
- cta_forced_reduction Boolean Computed "((payload->'cta')->>'forced_reduction')::boolean"
- crash_trigger_fired Boolean Computed "((payload->'crash_trigger')->>'fired')::boolean"

These tests exercise the pure helper functions directly. CLI-level
verification is covered transitively at Phase 0.4 (POST /regime/scan
will fail to insert the cri_series row if the booleans are missing,
because Postgres raises an error when a Computed column references a
missing JSONB key for a non-nullable cast).
"""

from xenon.scanners.cri import crash_trigger, cta_exposure_model

# ── crash_trigger.fired ──────────────────────────────────────────


def test_crash_trigger_emits_fired_boolean_when_all_conditions_met():
    out = crash_trigger(spx_below_ma=True, realized_vol=30.0, cor1m=70.0)
    assert "fired" in out
    assert isinstance(out["fired"], bool)
    assert out["fired"] is True
    # legacy field preserved
    assert out["triggered"] is True


def test_crash_trigger_fired_false_when_any_condition_missing():
    # SPX above MA → not fired
    out = crash_trigger(spx_below_ma=False, realized_vol=30.0, cor1m=70.0)
    assert out["fired"] is False
    assert out["triggered"] is False


def test_crash_trigger_fired_aliases_triggered_for_partial_trigger():
    # Vol below 25% threshold → not fired even if SPX below MA + cor1m high
    out = crash_trigger(spx_below_ma=True, realized_vol=20.0, cor1m=70.0)
    assert out["fired"] == out["triggered"]


# ── cta.forced_reduction ─────────────────────────────────────────


def test_cta_emits_forced_reduction_true_when_vol_above_target():
    # vol_target = 10%; realized_vol = 25% → exposure = 40% → reduction = 60%
    out = cta_exposure_model(realized_vol=25.0)
    assert "forced_reduction" in out
    assert isinstance(out["forced_reduction"], bool)
    assert out["forced_reduction"] is True
    # legacy field preserved
    assert out["forced_reduction_pct"] == 60.0


def test_cta_emits_forced_reduction_false_when_vol_at_or_below_target():
    # realized_vol = 10% → exposure = 100% → reduction = 0%
    out = cta_exposure_model(realized_vol=10.0)
    assert out["forced_reduction"] is False
    assert out["forced_reduction_pct"] == 0.0


def test_cta_emits_forced_reduction_false_for_low_vol():
    # realized_vol = 5% → exposure capped at MAX (200%) → reduction = 0%
    out = cta_exposure_model(realized_vol=5.0)
    assert out["forced_reduction"] is False


def test_cta_emits_forced_reduction_false_for_invalid_input():
    # NaN / zero / negative → guard clause returns reduction=0 / forced_reduction=False
    import math

    out_nan = cta_exposure_model(realized_vol=float("nan"))
    assert out_nan["forced_reduction"] is False

    out_zero = cta_exposure_model(realized_vol=0.0)
    assert out_zero["forced_reduction"] is False
