"""Pure trigger-evaluation tests. Spec §3.1, §9."""
from __future__ import annotations

from xenon.execution.brackets.triggers import (
    apply_trail_after_activation,
    debit_to_close_at_credit_pct,
    mfe_update,
    pct_change,
    threshold_crossed_below,
)


def test_pct_change_basic():
    assert pct_change(current=90.0, anchor=100.0) == -0.10


def test_threshold_crossed_below():
    assert threshold_crossed_below(mark=4.0, threshold=5.0) is True
    assert threshold_crossed_below(mark=5.0, threshold=5.0) is True
    assert threshold_crossed_below(mark=5.01, threshold=5.0) is False


def test_mfe_update_increases_only():
    assert mfe_update(current_mfe=10.0, current_mark=11.0) == 11.0
    assert mfe_update(current_mfe=10.0, current_mark=9.0) == 10.0
    assert mfe_update(current_mfe=None, current_mark=8.0) == 8.0


def test_apply_trail_after_activation_inactive():
    """Mark below activation: no trigger, MFE still tracked."""
    fired, new_mfe = apply_trail_after_activation(
        anchor_price=10.0,
        current_mark=11.0,
        current_mfe=11.0,
        trail_pct=0.25,
        activation_pct=0.30,
    )
    assert fired is False
    assert new_mfe == 11.0


def test_apply_trail_after_activation_active_trail_held():
    """Mark above activation but within trail: no trigger."""
    fired, new_mfe = apply_trail_after_activation(
        anchor_price=10.0,
        current_mark=13.5,
        current_mfe=14.0,
        trail_pct=0.25,
        activation_pct=0.30,
    )
    assert fired is False
    assert new_mfe == 14.0


def test_apply_trail_after_activation_fires():
    """Once activated, mark dropped more than trail_pct from MFE."""
    fired, _new_mfe = apply_trail_after_activation(
        anchor_price=10.0,
        current_mark=10.4,
        current_mfe=14.0,
        trail_pct=0.25,
        activation_pct=0.30,
    )
    assert fired is True


def test_debit_to_close_at_credit_pct():
    """Credit spread: collected $1.00, close at 50%, close when debit <= $0.50."""
    assert (
        debit_to_close_at_credit_pct(
            debit_to_close=0.50,
            credit_received=1.00,
            close_at_credit_pct=0.50,
        )
        is True
    )
    assert (
        debit_to_close_at_credit_pct(
            debit_to_close=0.51,
            credit_received=1.00,
            close_at_credit_pct=0.50,
        )
        is False
    )
    assert (
        debit_to_close_at_credit_pct(
            debit_to_close=0.40,
            credit_received=1.00,
            close_at_credit_pct=0.50,
        )
        is True
    )
