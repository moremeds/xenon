"""Apex Data refresh — GitHub Action entrypoint.

Reads meta/universe.json from R2, fetches OHLCV from Massive, computes TA
indicators, writes Parquet back to R2. Two modes: incremental (append new
bars) and full (re-fetch ~2y). Later tasks fill in the refresh loop
(refresh_one — Task 6) and the parallel driver + manifest (run_refresh — Task 7).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Iterable, Iterator

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

logger = logging.getLogger(__name__)

_DEFAULT_TIMEFRAMES = ("1d", "1h")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apex data refresh (OHLCV + indicators)")
    parser.add_argument("--mode", choices=["incremental", "full"], required=True)
    parser.add_argument(
        "--timeframes",
        type=lambda s: s.split(","),
        default=list(_DEFAULT_TIMEFRAMES),
        help="Comma-separated list, e.g. 1d,1h",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write to data/apex_mirror_preview/ instead of R2 (wired in Task 6 per A17)",
    )
    parser.add_argument("--max-workers", type=int, default=10)
    return parser


def load_universe(r2) -> list[dict]:
    """Read meta/universe.json from R2 and return the `tickers` list.

    Each row carries symbol, name, tier, sector, marketCap, dollar_volume,
    turnover_rate, timeframes[] (authoritative timeframes for that ticker).
    """
    payload = r2.get_json("meta/universe.json")
    tickers = payload.get("tickers") or []
    if not tickers:
        raise RuntimeError("meta/universe.json has no tickers")
    return tickers


def expand_targets(
    universe: Iterable[dict],
    *,
    timeframes: tuple[str, ...] = _DEFAULT_TIMEFRAMES,
) -> Iterator[tuple[str, str]]:
    """Yield (ticker, timeframe) pairs — intersection of requested and ticker's own list."""
    tf_set = set(timeframes)
    for row in universe:
        symbol = row.get("symbol")
        allowed = set(row.get("timeframes") or [])
        if not symbol:
            continue
        for tf in tf_set & allowed:
            yield symbol, tf


def main(argv: list[str] | None = None) -> int:
    load_dotenv(_PROJECT_ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    args = build_parser().parse_args(argv)

    from scripts.ta_lib.r2_store import R2Store

    r2 = R2Store()
    universe = load_universe(r2)
    targets = list(expand_targets(universe, timeframes=tuple(args.timeframes)))
    logger.info("Loaded %d tickers; %d (ticker, tf) targets", len(universe), len(targets))
    logger.info("Mode=%s dry_run=%s workers=%d", args.mode, args.dry_run, args.max_workers)
    # Task 6 fills in the actual refresh loop via run_refresh().
    return 0


if __name__ == "__main__":
    sys.exit(main())
