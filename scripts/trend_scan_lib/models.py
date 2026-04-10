"""Trend scanner data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from scripts.scanner_lib.models import BaseScanCandidate


@dataclass
class TrendCandidate(BaseScanCandidate):
    """A ranked trend scan candidate with full indicator snapshot."""

    spot_price: float = 0.0
    indicators: dict[str, float] = field(default_factory=dict)
    suggested_trade: str = ""
    invalidation: float = 0.0
    holding_window: str = "5-15 trading days"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "direction": self.direction,
            "final_score": self.final_score,
            "scores": self.scores,
            "spot_price": self.spot_price,
            "indicators": self.indicators,
            "summaries": self.summaries,
            "suggested_trade": self.suggested_trade,
            "invalidation": self.invalidation,
            "flags": self.flags,
            "holding_window": self.holding_window,
        }
