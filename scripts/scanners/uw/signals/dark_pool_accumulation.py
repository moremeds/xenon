"""Tier 2 signal: Dark Pool Accumulation (confirmation-only, direction-neutral)."""
from __future__ import annotations

from typing import Optional

from xenon.analysis.models import TickerData
from scripts.scanners.uw.models import SignalHit

MIN_PRINTS = 3
MIN_PRINT_PREMIUM = 1_000_000
MAX_PRICE_SPREAD_PCT = 0.5


def detect(ticker: str, td: TickerData) -> Optional[SignalHit]:
    if not td.darkpool:
        return None
    prints = td.darkpool.get("data") if isinstance(td.darkpool, dict) else None
    if not isinstance(prints, list):
        return None

    large = [p for p in prints if float(p.get("premium") or 0) >= MIN_PRINT_PREMIUM]
    if len(large) < MIN_PRINTS:
        return None

    for anchor in large:
        anchor_price = float(anchor.get("price") or 0)
        if anchor_price <= 0:
            continue
        cluster = [
            p for p in large
            if abs(float(p.get("price") or 0) - anchor_price) / anchor_price * 100.0 <= MAX_PRICE_SPREAD_PCT
        ]
        if len(cluster) >= MIN_PRINTS:
            total_premium = sum(float(p.get("premium") or 0) for p in cluster)
            return SignalHit(
                ticker=ticker.upper(),
                signal_type="dark_pool_accumulation",
                tier=2,
                score=min(1.0, total_premium / 10_000_000.0),
                evidence={
                    "cluster_size": len(cluster),
                    "anchor_price": anchor_price,
                    "total_premium": total_premium,
                    "direction_neutral": True,
                },
                freshness="stale",
            )
    return None
