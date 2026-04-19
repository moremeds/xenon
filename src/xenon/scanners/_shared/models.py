"""Base models shared across all scanners."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class BaseSignalHit:
    """A single signal detection result."""

    ticker: str
    signal_type: str
    score: float
    evidence: dict[str, Any]

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"score must be between 0 and 1, got {self.score}")


@dataclass(frozen=True)
class BaseContextFlag:
    """Non-scoring contextual annotation."""

    ticker: str
    layer: str
    label: str
    value: float


@dataclass
class BaseScanCandidate:
    """A ranked scan result."""

    ticker: str
    direction: Literal["bullish", "bearish"]
    final_score: float
    scores: dict[str, float]
    flags: list[str] = field(default_factory=list)
    summaries: dict[str, str] = field(default_factory=dict)
