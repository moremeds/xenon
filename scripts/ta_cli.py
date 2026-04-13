#!/usr/bin/env python3.13
"""Manual test CLI for TAService.

Usage:
    # With live IB Gateway:
    python3.13 scripts/ta_cli.py AAPL MSFT SPY

    # Show full indicator history (not just snapshot):
    python3.13 scripts/ta_cli.py AAPL --history

    # Bulk refresh then snapshot:
    python3.13 scripts/ta_cli.py AAPL MSFT --refresh

    # Use a custom DB path (e.g. temp for testing):
    python3.13 scripts/ta_cli.py AAPL --db /tmp/test_ta.duckdb

    # Dry run with no IB (read cache only):
    python3.13 scripts/ta_cli.py AAPL --cache-only

    # Query the DuckDB directly:
    python3.13 scripts/ta_cli.py --query "SELECT ticker, COUNT(*) as bars FROM ohlc_bars GROUP BY ticker"
    python3.13 scripts/ta_cli.py --query "SELECT * FROM ta_indicators WHERE ticker='AAPL' ORDER BY bar_date DESC LIMIT 5"

    # DB stats (row counts, tickers cached, date ranges):
    python3.13 scripts/ta_cli.py --stats
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Ensure project root on sys.path
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def main(argv=None):
    parser = argparse.ArgumentParser(description="TA-Lib manual test CLI")
    parser.add_argument("tickers", nargs="*", help="Ticker symbols (e.g. AAPL MSFT SPY)")
    parser.add_argument("--history", action="store_true", help="Show full indicator DataFrame instead of snapshot")
    parser.add_argument("--refresh", action="store_true", help="Run bulk_refresh before reading")
    parser.add_argument("--timeframe", "-tf", default="1d", choices=["1d", "1h"], help="Timeframe (default: 1d)")
    parser.add_argument("--db", default="data/ta.duckdb", help="DuckDB path (default: data/ta.duckdb)")
    parser.add_argument("--cache-only", action="store_true", help="Read cache only, no IB connection")
    parser.add_argument("--query", type=str, help="Run raw SQL against the DuckDB")
    parser.add_argument("--stats", action="store_true", help="Show DB stats (row counts, tickers, date ranges)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    # --query and --stats modes: direct DuckDB access, no IB needed
    if args.query or args.stats:
        import duckdb

        db_path = Path(args.db)
        if not db_path.exists():
            print(f"Database not found: {args.db}")
            return 1
        conn = duckdb.connect(str(db_path), read_only=True)

        if args.stats:
            print(f"Database: {args.db}")
            print(f"{'=' * 60}")
            try:
                ohlc_count = conn.execute("SELECT COUNT(*) FROM ohlc_bars").fetchone()[0]
                ind_count = conn.execute("SELECT COUNT(*) FROM ta_indicators").fetchone()[0]
                print(f"  ohlc_bars rows:      {ohlc_count:,}")
                print(f"  ta_indicators rows:  {ind_count:,}")

                tickers = conn.execute(
                    "SELECT ticker, timeframe, COUNT(*) as bars, "
                    "MIN(bar_date) as first_bar, MAX(bar_date) as last_bar, "
                    "MAX(fetched_at) as last_fetch "
                    "FROM ohlc_bars GROUP BY ticker, timeframe ORDER BY ticker"
                ).fetchdf()
                if len(tickers) > 0:
                    print(f"\n  Cached tickers ({len(tickers)}):")
                    print(tickers.to_string(index=False))
                else:
                    print("\n  No tickers cached yet.")
            except Exception as e:
                print(f"  Error reading stats: {e}")

        if args.query:
            print(f"\nSQL: {args.query}")
            print(f"{'=' * 60}")
            try:
                result = conn.execute(args.query).fetchdf()
                print(result.to_string(index=False) if len(result) > 0 else "(no rows)")
            except Exception as e:
                print(f"ERROR: {e}")

        conn.close()
        return 0

    if not args.tickers:
        parser.error("tickers required (or use --query/--stats)")

    from scripts.ta_lib.service import TAService

    ib_client = None
    if not args.cache_only:
        try:
            from scripts.clients.ib_client import IBClient

            ib_client = IBClient()
            ib_client.connect(client_id="auto")
            print("IB Gateway connected")
        except Exception as e:
            print(f"IB Gateway not available: {e}")
            if not args.cache_only:
                print("  Use --cache-only to read from cached data")
                return 1

    svc = TAService(db_path=args.db, ib_client=ib_client)
    print(f"TAService initialized (db: {args.db})")

    tf = args.timeframe

    if args.refresh:
        print(f"\nRefreshing {len(args.tickers)} tickers ({tf})...")
        svc.bulk_refresh(args.tickers, timeframe=tf)
        print("Bulk refresh complete")

    for ticker in args.tickers:
        print(f"\n{'=' * 60}")
        print(f"  {ticker} ({tf})")
        print(f"{'=' * 60}")

        try:
            if args.history:
                df = svc.get_indicators(ticker, timeframe=tf, allow_fetch=not args.cache_only)
                if df.empty:
                    print("  (no data)")
                    continue
                print(f"  Rows: {len(df)}")
                print(f"  Date range: {df['date'].iloc[0]} -> {df['date'].iloc[-1]}")
                print(f"\n  Last 5 rows:")
                cols = ["date", "close", "sma_20", "sma_50", "rsi_14", "adx_14", "macd", "bb_width", "atr_14"]
                display_cols = [c for c in cols if c in df.columns]
                print(df[display_cols].tail().to_string(index=False))
            else:
                snap = svc.get_snapshot(ticker, timeframe=tf, allow_fetch=not args.cache_only)
                print(f"  close:      {snap['close']:>10.2f}")
                print(f"  price:      {snap['price']:>10.2f}")
                print(f"  ma_20:      {snap.get('ma_20', 0):>10.2f}")
                print(f"  ma_50:      {snap.get('ma_50', 0):>10.2f}")
                print(f"  ma_200:     {snap.get('ma_200', 0):>10.2f}")
                print(f"  rsi:        {snap.get('rsi', 0):>10.1f}")
                print(f"  adx:        {snap.get('adx', 0):>10.1f}")
                print(f"  macd:       {snap.get('macd', 0):>10.4f}")
                print(f"  macd_hist:  {snap.get('macd_histogram', 0):>10.4f}")
                print(f"  bbw:        {snap.get('bbw', 0):>10.4f}")
                print(f"  atr_pct:    {snap.get('atr_pct', 0):>10.4f}")
                print(f"  high_52w:   {snap.get('high_52w', 0):>10.2f}")
                print(f"  avg_vol:    {snap.get('avg_20d_volume', 0):>12,.0f}")
                print(f"  dollar_vol: {snap.get('dollar_volume', 0):>12,.0f}")
                print(f"  up_ratio:   {snap.get('recent_up_ratio', 0):>10.2f}")
                print(f"  range_20d:  {snap.get('range_20d_pct', 0):>10.4f}")
                print(f"  ma20_trend: {snap.get('ma_20_series', [])}")

        except RuntimeError as e:
            print(f"  ERROR: {e}")
        except Exception as e:
            print(f"  UNEXPECTED: {e}")
            if args.verbose:
                import traceback

                traceback.print_exc()

    if ib_client is not None:
        try:
            ib_client.disconnect()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
