"""Context gates for signal quality.

These are NOT the Four Gates (convexity / edge / Kelly / naked-short).
They are signal-quality filters applied before a signal is emitted.
"""
from __future__ import annotations

from typing import Optional


def earnings_gate(*, earnings_within_14d: bool, window_days: int = 14) -> bool:
    """Pass if no earnings within the specified window.

    v1 only tracks within_14d, so callers requesting a shorter window get
    the conservative answer (treated as 14d).
    """
    return not earnings_within_14d


def liquidity_gate(*, option_volume: Optional[int], min_volume: int = 1000) -> bool:
    """Pass if option volume meets the minimum threshold."""
    if option_volume is None:
        return False
    return option_volume >= min_volume


def regime_gate(*, regime: str) -> bool:
    """Pass unless market regime is R2 (risk-off)."""
    return regime != "R2"
