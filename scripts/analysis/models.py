"""Frozen dataclass types for the analysis library.

All fields that can legitimately be None due to missing-data degradation
are typed Optional. See the Missing-data policy in the design doc.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Optional


@dataclass(frozen=True)
class VRPState:
    vrp_raw: Optional[float]
    vrp_zscore: Optional[float]
    iv_percentile: Optional[float]
    ts_ratio: Optional[float]
    ts_inverted: Optional[bool]
    earnings_within_14d: bool
    data_freshness: Literal["live", "stale", "unavailable"]


@dataclass(frozen=True)
class RegimeState:
    regime: Literal["R0", "R1", "R2"]
    reason: str
    gex_sign: Optional[Literal["positive", "negative", "mixed"]]
    gex_flip_relative: Optional[Literal["above_price", "below_price", "at_price"]]
    flip_distance_pct: Optional[float]


@dataclass(frozen=True)
class BucketScores:
    market_structure: float
    volatility: float
    flow: float
    positioning: float
    composite: float
    grade: Literal["A", "B", "C"]
    bias: Literal[
        "STRONGLY_BULLISH", "BULLISH", "MIXED", "BEARISH", "STRONGLY_BEARISH"
    ]
    mode: Literal["full", "fast"]
    reweighted: bool
    skipped_buckets: list[str]


@dataclass(frozen=True)
class BenchmarkSnapshot:
    ticker: str
    iv_rank: Optional[float]
    gex_regime: Optional[Literal["positive", "negative", "mixed"]]
    gex_flip: Optional[float]
    price: Optional[float]
    data_date: Optional[str]
    freshness: Literal["live", "stale", "unavailable"]


@dataclass(frozen=True)
class BenchmarkContext:
    spy: BenchmarkSnapshot
    sector_etf: Optional[BenchmarkSnapshot]


@dataclass(frozen=True)
class TickerData:
    ticker: str
    price: Optional[float]
    fetched_at: datetime
    # Market Structure bucket
    gex: Optional[dict]
    gex_by_strike: Optional[dict]
    # Volatility bucket
    iv: Optional[float]
    rv: Optional[float]
    iv_percentile: Optional[float]
    term_structure: Optional[list[dict]]
    rr_skew_25d: Optional[float]
    vrp_history: Optional[list[float]]
    # Flow bucket
    flow_alerts: Optional[list[dict]]
    net_premium: Optional[dict]
    pcr: Optional[float]
    darkpool: Optional[dict]
    # Positioning bucket
    oi_changes: Optional[list[dict]]
    short_interest: Optional[dict]
    # Context
    earnings_date: Optional[date]
    earnings_within_14d: bool

    def bucket_available(
        self,
        bucket: Literal[
            "market_structure", "volatility", "flow", "positioning"
        ],
    ) -> bool:
        if bucket == "market_structure":
            return self.gex is not None and self.gex_by_strike is not None
        if bucket == "volatility":
            return (
                self.iv is not None
                and self.iv_percentile is not None
                and self.term_structure is not None
            )
        if bucket == "flow":
            return self.flow_alerts is not None or self.net_premium is not None
        if bucket == "positioning":
            # v1 LIMITATION: positioning always unavailable — OI history and
            # short interest scoring are deferred. Bucket is always reweighted out.
            return False
        raise ValueError(f"unknown bucket: {bucket}")


@dataclass(frozen=True)
class AnalysisReport:
    ticker: str
    price: Optional[float]
    fetched_at: str
    data_freshness: dict[str, str]
    benchmark: BenchmarkContext
    vrp: VRPState
    regime: RegimeState
    scores: BucketScores
    notes: list[str]
