"""Type F (multi-signal confluence) detection."""
from __future__ import annotations

from scripts.uw_scan_lib.models import SignalHit

CONFLUENCE_WEIGHTS = {1: 3.0, 2: 1.5}


def is_type_f(hits: list[SignalHit]) -> bool:
    non_dp_types = {
        h.signal_type for h in hits
        if h.signal_type != "dark_pool_accumulation"
    }
    return len(non_dp_types) >= 2


def compute_confluence(hits: list[SignalHit]) -> float:
    return sum(CONFLUENCE_WEIGHTS.get(h.tier, 0.0) for h in hits)
