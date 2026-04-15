"""Bulk re-seed the TA DuckDB cache from Massive. Replaces ta_seed_yahoo.py.

Usage:
  python3.13 scripts/ta_reseed_massive.py \
      --timeframes 1d,1h --lookback-days 730 --tickers AAPL,SPY
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Sequence

from scripts.clients.massive_client import MassiveClient, MassiveError
from scripts.ta_lib.bars import fetch_bars
from scripts.ta_lib.indicators import compute_all
from scripts.ta_lib.store import get_connection, init_schema, write_indicators, write_ohlc

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Re-seed TA DuckDB from Massive")
    p.add_argument("--db", default="data/ta.duckdb")
    p.add_argument("--timeframes", default="1d,1h", help="Comma-separated. Supported: 1d, 1h")
    p.add_argument("--lookback-days", type=int, default=730, help="Calendar days of history to request")
    p.add_argument("--tickers", default="", help="Comma-separated override. Default uses trend-scan universe.")
    p.add_argument("--sp500", default="data/universe/sp500.json")
    p.add_argument("--nasdaq100", default="data/universe/nasdaq100.json")
    p.add_argument("--dry-run", action="store_true", help="Don't write; just list what would fetch")
    return p.parse_args(list(argv))


def _resolve_universe(override: str, sp500_path: str, nasdaq100_path: str) -> list[str]:
    if override:
        return [t.strip().upper() for t in override.split(",") if t.strip()]
    from scripts.trend_scan_lib.universe import build_static_universe

    return build_static_universe(sp500_path=sp500_path, nasdaq100_path=nasdaq100_path)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    tickers = _resolve_universe(args.tickers, args.sp500, args.nasdaq100)
    logger.info(
        "Re-seeding %d tickers x %d timeframes from Massive (lookback=%dd)",
        len(tickers),
        len(timeframes),
        args.lookback_days,
    )

    if args.dry_run:
        for t in tickers:
            for tf in timeframes:
                logger.info("DRY: would fetch %s %s lookback=%dd", t, tf, args.lookback_days)
        return 0

    massive = MassiveClient()
    conn = get_connection(args.db)
    init_schema(conn)

    failed: list[tuple[str, str, str]] = []
    for t in tickers:
        for tf in timeframes:
            try:
                df = fetch_bars(massive, t, timeframe=tf, lookback_days=args.lookback_days, overlap_days=0)
                if df.empty:
                    raise RuntimeError("empty frame")
                write_ohlc(conn, t, tf, df)
                indicators = compute_all(df)
                indicators["bar_date"] = indicators["date"]
                write_indicators(conn, t, tf, indicators)
                conn.commit()
            except MassiveError as exc:
                logger.warning("skip %s %s: %s", t, tf, exc)
                failed.append((t, tf, str(exc)))
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass  # no active transaction (DuckDB autocommit)
                logger.warning("error %s %s: %s", t, tf, exc)
                failed.append((t, tf, str(exc)))

    logger.info("Done. %d failures.", len(failed))
    if failed:
        logger.warning("First 10 failed entries: %s", failed[:10])
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
