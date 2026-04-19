"""Shared options-flow summarizer.

Consumes a list of flow alerts (UW /api/option-trades/flow-alerts shape) and
emits a directional bias summary. Extracted from scripts/fetch_flow.py so that
both /flow-analysis and /uw-analyze pipelines share the same scoring.
"""

from __future__ import annotations

from typing import Iterable


def summarize_options_flow(alerts: Iterable[dict]) -> dict:
    """Summarize options flow alerts for directional bias.

    Five-state bias enum (same shape as fetch_flow.analyze_options_flow):
        STRONGLY_BULLISH, BULLISH, NEUTRAL, BEARISH, STRONGLY_BEARISH
        (+ NO_DATA, ALL_CALLS sentinel values when inputs are degenerate)
    """
    alerts = list(alerts or [])
    if not alerts:
        return {
            "total_alerts": 0,
            "total_premium": 0,
            "call_premium": 0,
            "put_premium": 0,
            "call_put_ratio": None,
            "bias": "NO_DATA",
        }

    call_premium = 0.0
    put_premium = 0.0
    for a in alerts:
        prem = float(a.get("premium", 0))
        if a.get("is_call"):
            call_premium += prem
        else:
            put_premium += prem

    total = call_premium + put_premium
    cp_ratio = round(call_premium / put_premium, 2) if put_premium > 0 else None

    if cp_ratio is None:
        bias = "ALL_CALLS" if call_premium > 0 else "NO_DATA"
    elif cp_ratio >= 2.0:
        bias = "STRONGLY_BULLISH"
    elif cp_ratio >= 1.2:
        bias = "BULLISH"
    elif cp_ratio <= 0.5:
        bias = "STRONGLY_BEARISH"
    elif cp_ratio <= 0.8:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    return {
        "total_alerts": len(alerts),
        "total_premium": round(total, 2),
        "call_premium": round(call_premium, 2),
        "put_premium": round(put_premium, 2),
        "call_put_ratio": cp_ratio,
        "bias": bias,
    }
