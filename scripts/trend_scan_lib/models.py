"""Trend scanner data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from scanners._shared.models import BaseScanCandidate


@dataclass
class TrendCandidate(BaseScanCandidate):
    """A ranked trend scan candidate.

    ANALYSIS-ONLY: this object describes signal, not a trade. The
    `structure_hint` field suggests an options structure that *might*
    fit the signal's convexity profile, but Four Gates (convexity
    arithmetic, edge validation, Kelly sizing, no-naked-shorts) are NOT
    applied here — that happens at order-routing time.

    The 'four_gates_not_applied' flag is auto-added to every candidate
    so downstream consumers cannot miss this."""

    spot_price: float = 0.0
    indicators: dict[str, float] = field(default_factory=dict)
    structure_hint: str = ""  # informational only
    invalidation: float = 0.0
    holding_window: str = "5-15 trading days"
    catalysts: list[str] = field(default_factory=list)  # populated by Stage C (Task 10)

    def __post_init__(self):
        if "four_gates_not_applied" not in self.flags:
            self.flags.append("four_gates_not_applied")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "direction": self.direction,
            "final_score": self.final_score,
            "scores": self.scores,
            "spot_price": self.spot_price,
            "indicators": self.indicators,
            "summaries": self.summaries,
            "structure_hint": self.structure_hint,
            "invalidation": self.invalidation,
            "flags": self.flags,
            "holding_window": self.holding_window,
            "catalysts": self.catalysts,
        }
