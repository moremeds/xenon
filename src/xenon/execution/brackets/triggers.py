"""Pure trigger arithmetic. No I/O, no DB. Spec §9."""
from __future__ import annotations


def pct_change(*, current: float, anchor: float) -> float:
    if anchor == 0:
        raise ValueError("anchor cannot be zero")
    return (current - anchor) / anchor


def threshold_crossed_below(*, mark: float, threshold: float) -> bool:
    return mark <= threshold


def mfe_update(*, current_mfe: float | None, current_mark: float) -> float:
    if current_mfe is None:
        return current_mark
    return max(current_mfe, current_mark)


def apply_trail_after_activation(
    *,
    anchor_price: float,
    current_mark: float,
    current_mfe: float | None,
    trail_pct: float,
    activation_pct: float,
) -> tuple[bool, float]:
    """Return (fired, new_mfe) for trailing take-profit evaluation."""
    new_mfe = mfe_update(current_mfe=current_mfe, current_mark=current_mark)
    activation_level = anchor_price * (1 + activation_pct)
    if new_mfe < activation_level:
        return False, new_mfe

    trail_level = new_mfe * (1 - trail_pct)
    return current_mark < trail_level, new_mfe


def debit_to_close_at_credit_pct(
    *,
    debit_to_close: float,
    credit_received: float,
    close_at_credit_pct: float,
) -> bool:
    """Close when debit_to_close <= (1 - close_at_credit_pct) * credit_received."""
    return debit_to_close <= (1 - close_at_credit_pct) * credit_received
