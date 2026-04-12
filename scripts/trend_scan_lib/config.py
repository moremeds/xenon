"""Trend scanner configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class TrendScanConfig:
    """Configuration for the trend scanner pipeline."""

    top_n: int = 25
    max_workers: int = 15

    # Scoring weights — must sum to 1.0
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "trend": 0.35,
            "structure": 0.25,
            "volatility": 0.20,
            "flow": 0.20,
        }
    )

    # Minimum scores to pass final ranking
    min_thresholds: dict[str, float] = field(
        default_factory=lambda: {
            "trend": 0.4,
            "structure": 0.3,
        }
    )

    # Universe floor filters
    min_market_cap: float = 1_000_000_000
    min_dollar_volume: float = 10_000_000
    min_price: float = 5.0

    # Universe source paths (absolute, resolved from project root)
    sp500_path: str = str(_PROJECT_ROOT / "data" / "universe" / "sp500.json")
    nasdaq100_path: str = str(_PROJECT_ROOT / "data" / "universe" / "nasdaq100.json")

    # UW flow alert filters for universe source
    uw_flow_min_premium: float = 100_000
    uw_flow_lookback_days: int = 5
