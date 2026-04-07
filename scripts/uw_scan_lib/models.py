"""uw-scan dataclasses."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class SignalHit:
    ticker: str
    signal_type: str
    tier: Literal[1, 2]
    score: float
    evidence: dict[str, Any]
    freshness: Literal["live", "stale", "unavailable"] = "live"


@dataclass(frozen=True)
class ContextFlag:
    ticker: str
    layer: str
    label: str
    value: float


@dataclass
class ScanCandidate:
    ticker: str
    hits: list[SignalHit]
    context_flags: list[ContextFlag]
    raw_score: float
    confluence_score: float
    final_score: float
    is_type_f: bool
    gates: dict[str, str] = field(default_factory=dict)
