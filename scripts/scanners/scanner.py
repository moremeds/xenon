#!/usr/bin/env python3
"""
Scan watchlist for dark pool flow signals.
Ranks tickers by flow strength and filters for actionable signals.

API Reference: docs/reference/unusual_whales_api.md
Full Spec: docs/reference/unusual_whales_api_spec.yaml

Uses fetch_flow.py internally which calls:
  - GET /api/darkpool/{ticker} - Dark pool flow data
"""

import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from xenon.clients.uw_client import UWRateLimitError
from fetchers.fetch_flow import fetch_flow as fetch_flow_module
from xenon.analysis.dark_pool_summary import summarize_dark_pool

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SCRIPT_DIR.parent
WATCHLIST = PROJECT_DIR / "data" / "watchlist.json"
PORTFOLIO = PROJECT_DIR / "data" / "portfolio.json"


def get_open_positions():
    """Get list of tickers with open positions."""
    if not PORTFOLIO.exists():
        return set()
    with open(PORTFOLIO) as f:
        portfolio = json.load(f)
    return {p["ticker"] for p in portfolio.get("positions", [])}


def fetch_flow_data(ticker: str, days: int = 5) -> dict:
    """Fetch flow data for a single ticker via the shared wrapper seam."""
    try:
        return fetch_flow_module(ticker, lookback_days=days, skip_options_flow=True)
    except Exception as e:
        return {"error": str(e)}


# Keep old name as alias so existing call sites work
fetch_flow = fetch_flow_data


def analyze_signal(flow_data: dict) -> dict:
    """Extract key metrics from flow data.

    Thin wrapper around analysis.dark_pool_summary.summarize_dark_pool; kept as
    a name alias because many call sites (and tests) import `analyze_signal`
    directly from scanner.
    """
    return summarize_dark_pool(flow_data)


def _process_ticker(item: dict, client=None) -> dict:
    """Process a single ticker: fetch flow and analyze signal.

    Returns a result dict or None on error.
    Designed to run inside a ThreadPoolExecutor worker.

    Args:
        item: Watchlist item with 'ticker' key
        client: Optional shared UWClient (passed via functools.partial)
    """
    ticker = item["ticker"]
    try:
        # Use the wrapper seam so tests and callers can patch scanner.fetch_flow_data
        # without needing to know the internal fetch_flow import path.
        flow = fetch_flow_data(ticker, days=5)
        analysis = analyze_signal(flow)
        return {"ticker": ticker, "sector": item.get("sector", "Unknown"), **analysis}
    except UWRateLimitError:
        logger.warning("Rate limited on %s — skipping", ticker)
        print(f"  {ticker} - SKIP (rate limited)", file=sys.stderr)
        return None
    except Exception as exc:
        logger.warning("Error processing %s: %s", ticker, exc)
        print(f"  {ticker} - ERROR ({exc})", file=sys.stderr)
        return None


def scan(top_n: int = 20, min_score: float = 0, max_workers: int = 5):
    """Scan all watchlist tickers and rank by signal strength.

    Uses ThreadPoolExecutor to process tickers concurrently.

    Args:
        top_n: Number of top signals to return.
        min_score: Minimum score threshold.
        max_workers: Maximum concurrent workers (default 15).
    """
    if not WATCHLIST.exists():
        print(json.dumps({"error": "No watchlist.json found"}))
        return

    with open(WATCHLIST) as f:
        watchlist = json.load(f)

    open_positions = get_open_positions()
    tickers = watchlist.get("tickers", [])

    # Filter out open positions before dispatching to workers
    items_to_scan = [item for item in tickers if item["ticker"] not in open_positions]
    skipped = len(tickers) - len(items_to_scan)
    if skipped:
        print(f"Skipping {skipped} tickers with open positions", file=sys.stderr)

    print(f"Scanning {len(items_to_scan)} tickers ({max_workers} workers)...", file=sys.stderr)

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_process_ticker, item): item for item in items_to_scan}
        done = 0
        for future in as_completed(futures):
            done += 1
            item = futures[future]
            ticker = item["ticker"]
            try:
                result = future.result()
            except Exception as exc:
                logger.warning("Unhandled error for %s: %s", ticker, exc)
                print(f"  [{done}/{len(items_to_scan)}] {ticker} - ERROR ({exc})", file=sys.stderr)
                continue
            if result is not None:
                print(
                    f"  [{done}/{len(items_to_scan)}] {ticker}... {result['signal']} ({result['score']})",
                    file=sys.stderr,
                )
                results.append(result)

    # Sort by score descending
    results.sort(key=lambda x: x["score"], reverse=True)

    # Filter by min_score and take top_n
    filtered = [r for r in results if r["score"] >= min_score][:top_n]

    output = {
        "scan_time": datetime.now().isoformat(),
        "tickers_scanned": len(results),
        "signals_found": len([r for r in results if r["signal"] in ("STRONG", "MODERATE")]),
        "top_signals": filtered,
    }

    print(json.dumps(output, indent=2))


def main():
    import argparse

    p = argparse.ArgumentParser(description="Scan watchlist for flow signals")
    p.add_argument("--top", type=int, default=20, help="Number of top signals to show")
    p.add_argument("--min-score", type=float, default=0, help="Minimum score threshold")
    p.add_argument("--workers", type=int, default=15, help="Max concurrent workers (default 15)")
    args = p.parse_args()

    scan(top_n=args.top, min_score=args.min_score, max_workers=args.workers)


if __name__ == "__main__":
    main()
