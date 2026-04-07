#!/usr/bin/env python3
"""
Analyze dark pool flow for all open portfolio positions.
Categorizes each position based on whether flow supports or contradicts the trade direction.

Output: JSON to stdout with supports/against/watch/neutral arrays.

Source-agnostic: uses ``utils.portfolio_adapter`` to load IB or Futu positions
into a common shape. Select with ``--account ib|futu`` (default ``ib``).
"""
import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fetch_flow import fetch_flow as fetch_flow_module
from scanner import analyze_signal
from utils.portfolio_adapter import group_by_ticker, load_normalized_positions

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent


def classify_position(pos: dict, flow_data: dict, analysis: dict) -> dict:
    """Classify a single position based on flow vs position direction.

    ``pos`` is the dict form of a NormalizedPosition (ticker/direction/structure/qty/raw).
    """
    ticker = pos["ticker"]
    pos_direction = pos.get("direction", "LONG").upper()
    structure = pos.get("structure", "Unknown")
    signal = analysis.get("signal", "NONE")
    flow_dir = analysis.get("direction", "UNKNOWN")
    strength = analysis.get("strength", 0)
    buy_ratio = analysis.get("buy_ratio")
    sustained = analysis.get("sustained_days", 0)
    recent_dir = analysis.get("recent_direction", "UNKNOWN")

    # Determine flow label and CSS class
    if buy_ratio is not None:
        pct = int(buy_ratio * 100) if isinstance(buy_ratio, float) else buy_ratio
        if flow_dir == "ACCUMULATION":
            flow_label = f"{pct}% ACCUM"
            flow_class = "accum"
        elif flow_dir == "DISTRIBUTION":
            flow_label = f"{100 - pct}% DISTRIB"
            flow_class = "distrib"
        else:
            flow_label = f"{pct}% NEUTRAL"
            flow_class = "neutral"
    else:
        flow_label = "NO DATA"
        flow_class = "neutral"

    # Generate note based on signal
    note = ""
    if signal == "STRONG":
        if sustained >= 3:
            note = f"Strong signal, {sustained}-day sustained {flow_dir.lower()}"
        else:
            note = f"Strong institutional {flow_dir.lower()}"
    elif signal == "MODERATE":
        if recent_dir != flow_dir and recent_dir in ("ACCUMULATION", "DISTRIBUTION"):
            note = f"Mixed: aggregate {flow_dir.lower()}, recent {recent_dir.lower()}"
        else:
            note = f"Moderate {flow_dir.lower()} signal"
    elif signal == "WEAK":
        note = f"Weak {flow_dir.lower()} signal"
    else:
        note = "No actionable signal"

    # Categorize: supports, against, watch, neutral
    is_long = pos_direction in ("LONG", "BUY", "DEBIT")
    is_short = pos_direction in ("SHORT", "SELL", "CREDIT")

    if signal in ("STRONG", "MODERATE"):
        flow_supports_long = flow_dir == "ACCUMULATION"
        flow_supports_short = flow_dir == "DISTRIBUTION"

        if (is_long and flow_supports_long) or (is_short and flow_supports_short):
            category = "supports"
        elif (is_long and flow_supports_short) or (is_short and flow_supports_long):
            category = "against"
        else:
            category = "neutral"
    elif signal == "MODERATE" and recent_dir != flow_dir and recent_dir in ("ACCUMULATION", "DISTRIBUTION"):
        category = "watch"
    elif signal == "WEAK" and flow_dir in ("ACCUMULATION", "DISTRIBUTION"):
        # Weak but directional — watch if conflicting with recent
        if recent_dir != flow_dir and recent_dir in ("ACCUMULATION", "DISTRIBUTION"):
            category = "watch"
        else:
            category = "neutral"
    else:
        category = "neutral"

    # Extract daily buy_ratio series (oldest → newest)
    daily_buy_ratios = []
    dp_daily = flow_data.get("dark_pool", {}).get("daily", [])
    for day in sorted(dp_daily, key=lambda d: d.get("date", "")):
        daily_buy_ratios.append({
            "date": day.get("date", ""),
            "buy_ratio": day.get("dp_buy_ratio"),
        })

    return {
        "ticker": ticker,
        "position": structure,
        "direction": pos_direction,
        "flow_direction": flow_dir,
        "flow_label": flow_label,
        "flow_class": flow_class,
        "strength": round(strength, 1),
        "buy_ratio": buy_ratio,
        "daily_buy_ratios": daily_buy_ratios,
        "note": note,
        "category": category,
    }


def _empty_output(scanned: int = 0, skipped: int = 0, account: str = "ib") -> dict:
    return {
        "analysis_time": datetime.now().isoformat(),
        "account": account,
        "positions_scanned": scanned,
        "skipped_unsupported": skipped,
        "supports": [],
        "against": [],
        "watch": [],
        "neutral": [],
    }


def run_analysis(account: str = "ib"):
    """Run flow analysis for all portfolio positions for the given account."""
    loaded = load_normalized_positions(account)  # type: ignore[arg-type]
    positions = [asdict(p) for p in loaded.positions]

    if not positions:
        print(json.dumps(_empty_output(0, loaded.skipped_unsupported, account), indent=2))
        return

    print(
        f"Analyzing flow for {len(positions)} positions ({account}, "
        f"{loaded.skipped_unsupported} skipped)...",
        file=sys.stderr,
    )

    # Per-ticker dedup: fetch flow once per unique symbol.
    by_ticker = group_by_ticker(loaded.positions)
    flow_cache: dict = {}
    analysis_cache: dict = {}
    for i, ticker in enumerate(sorted(by_ticker.keys()), 1):
        print(f"  [{i}/{len(by_ticker)}] {ticker}...", file=sys.stderr, end=" ")
        try:
            flow_data = fetch_flow_module(ticker)
            analysis = analyze_signal(flow_data)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            analysis = {
                "score": -1, "signal": "ERROR", "direction": "UNKNOWN",
                "strength": 0, "buy_ratio": None, "sustained_days": 0,
                "recent_direction": "UNKNOWN", "recent_strength": 0,
            }
            flow_data = {}
        flow_cache[ticker] = flow_data
        analysis_cache[ticker] = analysis
        print(f"{analysis.get('signal', 'N/A')} ({analysis.get('score', 0)})", file=sys.stderr)

    results = {"supports": [], "against": [], "watch": [], "neutral": []}
    for pos in positions:
        ticker = pos["ticker"]
        classified = classify_position(pos, flow_cache.get(ticker, {}), analysis_cache.get(ticker, {}))
        category = classified.pop("category")
        results[category].append(classified)

    # Sort each category by strength descending
    for cat in results:
        results[cat].sort(key=lambda x: x["strength"], reverse=True)

    output = {
        "analysis_time": datetime.now().isoformat(),
        "account": account,
        "positions_scanned": len(positions),
        "skipped_unsupported": loaded.skipped_unsupported,
        **results,
    }

    print(json.dumps(output, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Run portfolio flow analysis.")
    parser.add_argument("--account", choices=["ib", "futu"], default="ib")
    args = parser.parse_args()
    run_analysis(args.account)


if __name__ == "__main__":
    main()
