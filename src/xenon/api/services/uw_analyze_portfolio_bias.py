"""Portfolio-centric flow alignment, backed by the uw-analyze cache.

Replaces the old ``scripts/flow_analysis.py`` classifier with a version
that:

1. Uses position *structure* (via utils.position_bias) instead of the
   LONG/SHORT sign — fixes the NVDA-class bug where a Long Put under
   dark-pool ACCUMULATION landed in ``supports``.

2. Reads dark-pool and options-flow summaries straight out of the shared
   uw-analyze cache so /flow-analysis and /uw-analyze don't each run their
   own UW API pipeline.

3. Emits five buckets (``supports``, ``against``, ``mixed``,
   ``non_directional``, ``neutral``) so non-directional structures
   (iron condor, collar, long straddle) stop being forced into a
   supports/against verdict they don't deserve.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import datetime
from typing import Any, Callable, Literal, Optional, Protocol

from xenon.utils.portfolio_adapter import (
    LoadResult,
    NormalizedPosition,
    group_by_ticker,
    load_normalized_positions,
)
from xenon.utils.position_bias import Bias, position_bias

logger = logging.getLogger("xenon.flow_analysis")

Verdict = Literal["supports", "against", "mixed", "non_directional", "neutral"]

# Cap fan-out to avoid hammering the UW API on cold-start refreshes.
_MAX_CONCURRENT_REFRESHES = 4


class _CacheLike(Protocol):
    def get_entry(self, ticker: str) -> Optional[dict]: ...

    async def get_or_run(
        self,
        ticker: str,
        *,
        runner: Optional[Callable[..., Any]] = None,
        force: bool = False,
        sources: Optional[list[str]] = None,
    ) -> tuple[dict, bool]: ...


def _options_side(bias: str) -> Literal["bullish", "bearish", "neutral"]:
    if bias in ("STRONGLY_BULLISH", "BULLISH", "ALL_CALLS"):
        return "bullish"
    if bias in ("STRONGLY_BEARISH", "BEARISH"):
        return "bearish"
    return "neutral"


def _dp_side(summary: dict) -> Literal["bullish", "bearish", "neutral"]:
    direction = (summary or {}).get("direction")
    signal = (summary or {}).get("signal", "NONE")
    if signal == "NONE" or direction not in ("ACCUMULATION", "DISTRIBUTION"):
        return "neutral"
    return "bullish" if direction == "ACCUMULATION" else "bearish"


def align(
    bias: Bias,
    dark_pool_summary: Optional[dict],
    options_flow_summary: Optional[dict],
) -> Verdict:
    """Return the alignment verdict for a position against the flow signals.

    Rules:
      * Non-directional position bias -> always ``non_directional``
      * Both signals agree with bias -> ``supports``
      * Both signals contradict bias -> ``against``
      * One agrees, the other disagrees or is silent -> ``mixed``
      * Both signals silent -> ``neutral``
    """
    if bias in ("neutral_vol", "income", "hedge", "unknown"):
        return "non_directional"

    dp = _dp_side(dark_pool_summary or {})
    of = _options_side((options_flow_summary or {}).get("bias", "NO_DATA"))

    pos_side = "bullish" if bias == "bullish" else "bearish"
    opp_side = "bearish" if pos_side == "bullish" else "bullish"

    agrees_dp = dp == pos_side
    agrees_of = of == pos_side
    contradicts_dp = dp == opp_side
    contradicts_of = of == opp_side

    if agrees_dp and agrees_of:
        return "supports"
    if contradicts_dp and contradicts_of:
        return "against"
    if dp == "neutral" and of == "neutral":
        return "neutral"
    # Everything else — at least one signal speaks, and the other either
    # disagrees or is silent. This is the resurrected "watch" case.
    return "mixed"


def _row_for(pos: NormalizedPosition, entry: dict) -> dict:
    bias = position_bias(
        {
            "ticker": pos.ticker,
            "direction": pos.direction,
            "structure": pos.structure,
            "qty": pos.qty,
            "raw": pos.raw,
        }
    )
    dps = entry.get("dark_pool_summary") if entry else None
    ofs = entry.get("options_flow_summary") if entry else None
    verdict = align(bias, dps, ofs)

    return {
        "ticker": pos.ticker,
        "structure": pos.structure,
        "direction": pos.direction,
        "bias": bias,
        "dark_pool": _dark_pool_display(dps),
        "options_flow": _options_flow_display(ofs),
        "alignment": verdict,
    }


def _dark_pool_display(summary: Optional[dict]) -> dict:
    if not summary:
        return {"direction": "NO_DATA", "strength": 0, "buy_ratio": None, "signal": "NONE"}
    return {
        "direction": summary.get("direction"),
        "strength": summary.get("strength", 0),
        "buy_ratio": summary.get("buy_ratio"),
        "signal": summary.get("signal", "NONE"),
    }


def _options_flow_display(summary: Optional[dict]) -> dict:
    if not summary:
        return {"bias": "NO_DATA", "call_put_ratio": None}
    return {
        "bias": summary.get("bias", "NO_DATA"),
        "call_put_ratio": summary.get("call_put_ratio"),
    }


async def classify_portfolio(
    account: str,
    cache: _CacheLike,
    runner: Optional[Callable[..., Any]] = None,
    load_positions: Callable[[str], LoadResult] = load_normalized_positions,
    read_only: bool = False,
) -> dict:
    """Build a per-position alignment view for the given account.

    When ``read_only`` is True, cache misses are left as empty rows instead
    of triggering a refresh — used by the GET path so initial page loads
    don't stall on a 5-second UW fetch.
    """
    loaded = load_positions(account)  # type: ignore[arg-type]
    positions = loaded.positions

    buckets: dict[str, list[dict]] = {
        "supports": [],
        "against": [],
        "mixed": [],
        "non_directional": [],
        "neutral": [],
    }

    if not positions:
        return {
            "analysis_time": datetime.now().isoformat(),
            "account": account,
            "positions_scanned": 0,
            "skipped_unsupported": loaded.skipped_unsupported,
            **buckets,
        }

    by_ticker = group_by_ticker(positions)

    # Fill cache misses concurrently (bounded).
    sem = asyncio.Semaphore(_MAX_CONCURRENT_REFRESHES)
    needs_refresh: list[str] = []
    for ticker in by_ticker:
        entry = cache.get_entry(ticker)
        if (not entry or not entry.get("dark_pool_summary")) and not read_only and runner is not None:
            needs_refresh.append(ticker)

    async def _fill(ticker: str) -> None:
        async with sem:
            try:
                await cache.get_or_run(ticker, runner=runner, sources=["portfolio"])
            except Exception as exc:  # noqa: BLE001
                logger.warning("flow_analysis cache fill failed for %s: %s", ticker, exc)

    if needs_refresh:
        await asyncio.gather(*(_fill(t) for t in needs_refresh))

    for ticker, ticker_positions in by_ticker.items():
        entry = cache.get_entry(ticker) or {}
        for pos in ticker_positions:
            row = _row_for(pos, entry)
            buckets[row["alignment"]].append(row)

    # Sort each bucket by ticker for deterministic rendering.
    for cat in buckets:
        buckets[cat].sort(key=lambda r: (r["ticker"], r["structure"]))

    return {
        "analysis_time": datetime.now().isoformat(),
        "account": account,
        "positions_scanned": len(positions),
        "skipped_unsupported": loaded.skipped_unsupported,
        **buckets,
    }
