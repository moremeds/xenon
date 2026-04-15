"""Seed TA DuckDB cache with 1 year of daily OHLCV from Yahoo Finance.

One-time exception to the IB-first data source policy (see scripts/CLAUDE.md).
Yahoo Finance is used here only as a bulk bootstrap source so that TA indicators
are available before the first IB market-hours fetch cycle populates the cache.
This script is intended to be run once; subsequent updates come from IB via
TAService.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import pandas as pd
import yfinance  # noqa: E402

from scripts.ta_lib.indicators import compute_all  # noqa: E402
from scripts.ta_lib.store import (  # noqa: E402
    DEFAULT_DB_PATH,
    get_connection,
    init_schema,
    read_ohlc,
    write_indicators,
    write_ohlc,
)
from scripts.trend_scan_lib.universe import build_static_universe  # noqa: E402

_COL_RENAME = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Volume": "volume",
}


def _extract_ticker_df(
    raw: pd.DataFrame,
    ticker: str,
    *,
    multi: bool,
) -> pd.DataFrame:
    """Pull a single-ticker OHLCV DataFrame from the bulk download result.

    Handles both MultiIndex columns (multi-ticker download) and flat columns
    (single-ticker download).
    """
    if multi:
        # yfinance group_by="ticker" → level 0 = ticker, level 1 = field
        if ticker not in raw.columns.get_level_values(0):
            return pd.DataFrame()
        sub = raw[ticker].copy()
    else:
        sub = raw.copy()

    # Drop Adj Close if present
    for col in list(sub.columns):
        if "adj" in str(col).lower():
            sub = sub.drop(columns=[col])

    sub = sub.rename(columns=_COL_RENAME)
    sub = sub.reset_index()

    # Normalise the date column name (yfinance may use 'Date' or 'date')
    for c in sub.columns:
        if str(c).lower() == "date":
            sub = sub.rename(columns={c: "date"})
            break

    return sub


def _normalize_ticker(ticker: str) -> str:
    """BRK.B → BRK B (DuckDB convention)."""
    return ticker.replace(".", " ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed TA DuckDB with Yahoo OHLCV")
    parser.add_argument("--dry-run", action="store_true", help="Print ticker count and exit")
    parser.add_argument("--tickers", nargs="+", default=None, help="Override universe")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="DuckDB path")
    args = parser.parse_args(argv)

    # --- Build universe ---
    if args.tickers:
        tickers = args.tickers
    else:
        project_root = Path(_project_root)
        tickers = build_static_universe(
            sp500_path=str(project_root / "data/universe/sp500.json"),
            nasdaq100_path=str(project_root / "data/universe/nasdaq100.json"),
        )
        if "SPY" not in tickers:
            tickers.append("SPY")

    if args.dry_run:
        print(f"Would seed {len(tickers)} tickers", file=sys.stderr)
        return 0

    # --- Bulk download ---
    # yfinance expects dots for tickers like BRK.B during download
    print(f"Downloading 1y daily OHLCV for {len(tickers)} tickers ...", file=sys.stderr)
    raw = yfinance.download(tickers, period="1y", interval="1d", group_by="ticker", threads=True)

    multi = len(tickers) > 1

    # --- DB setup ---
    conn = get_connection(args.db)
    init_schema(conn)

    ok = 0
    failures: list[str] = []

    for ticker in tickers:
        try:
            df = _extract_ticker_df(raw, ticker, multi=multi)
            if df.empty:
                failures.append(ticker)
                print(f"  SKIP {ticker}: empty DataFrame", file=sys.stderr)
                continue

            norm = _normalize_ticker(ticker)

            # Write OHLC
            write_ohlc(conn, norm, "1d", df)

            # Read back, compute indicators, write indicators
            ohlc = read_ohlc(conn, norm, "1d")
            if ohlc is None:
                failures.append(ticker)
                print(f"  SKIP {ticker}: no rows after write", file=sys.stderr)
                continue

            ohlc = ohlc.rename(columns={"bar_date": "date"})
            result = compute_all(ohlc)
            result["bar_date"] = result["date"]
            write_indicators(conn, norm, "1d", result)

            ok += 1
        except Exception as exc:
            failures.append(ticker)
            print(f"  FAIL {ticker}: {exc}", file=sys.stderr)

    conn.close()

    print(f"\nDone: {ok} ok, {len(failures)} failed", file=sys.stderr)
    if failures:
        print(f"Failures: {', '.join(failures)}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
