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


import io
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from scripts.ta_lib.bars import fetch_bars
from scripts.ta_lib.parquet_store import (
    INDICATOR_COLUMNS,
    dedupe_concat,
    read_ohlcv,
    write_indicators,
    write_ohlcv,
)
from scripts.ta_lib.r2_store import R2NotFoundError

_FULL_LOOKBACK_DAYS = 730  # 2 years for cold-start / full refresh


@dataclass
class RefreshResult:
    ticker: str
    timeframe: str
    succeeded: bool
    rows_written: int = 0
    error: str | None = None


def _next_start(last_ts: pd.Timestamp, timeframe: str) -> date:
    """Return the inclusive start date for the next Massive fetch.

    For 1h, use the date of last_ts itself (not last_ts + 1 day) and rely on
    dedupe_concat to drop already-stored bars (A11).
    For 1d, last_ts + 1 day avoids refetching yesterday.
    """
    if timeframe == "1h":
        return last_ts.date()
    return (last_ts + pd.Timedelta(days=1)).date()


def _compute_indicators_adapter(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """Compute the full indicator column set for parquet_store.

    Requires an OHLCV df with at least ['timestamp', 'open', 'high', 'low',
    'close', 'volume']. Uses full rolling min_periods (A10) so warmup rows are
    NaN. Includes A3 scanner-contract derived fields.
    """
    from scripts.ta_lib.indicators import compute_all

    working = ohlcv.set_index("timestamp").copy()
    enriched = compute_all(working).reset_index()

    # A10: full min_periods for derived high/low/volume features
    enriched["high_20d"] = enriched["high"].rolling(20, min_periods=20).max()
    enriched["low_20d"] = enriched["low"].rolling(20, min_periods=20).min()
    enriched["high_52w"] = enriched["high"].rolling(252, min_periods=252).max()
    enriched["low_52w"] = enriched["low"].rolling(252, min_periods=252).min()

    up_mask = (enriched["close"] >= enriched["open"]).astype(float)
    up_vol = up_mask * enriched["volume"]
    enriched["up_day_volume_ratio"] = (
        up_vol.rolling(20, min_periods=20).sum() / enriched["volume"].rolling(20, min_periods=20).sum()
    )

    # A3: scanner-contract derived fields
    enriched["recent_avg_volume"] = enriched["volume"].rolling(5, min_periods=5).mean()
    enriched["avg_20d_volume"] = enriched["volume"].rolling(20, min_periods=20).mean()
    enriched["recent_up_ratio"] = up_mask.rolling(20, min_periods=20).mean()
    enriched["range_20d_pct"] = (
        enriched["high"].rolling(20, min_periods=20).max() - enriched["low"].rolling(20, min_periods=20).min()
    ) / enriched["close"]
    enriched["atr_pct"] = enriched["atr_14"] / enriched["close"]

    # Project to canonical indicator schema
    return enriched.loc[:, list(INDICATOR_COLUMNS)]


def refresh_one(
    *,
    r2,
    massive,
    ticker: str,
    timeframe: str,
    mode: str,
) -> "RefreshResult":
    """Refresh a single (ticker, timeframe) pair. Never raises — returns RefreshResult.

    Full mode: fetch ~2y from Massive, write both parquets.
    Incremental mode: read existing historical parquet, fetch just the gap since
    last bar, dedupe, rewrite.

    Per A5 atomicity: compute BOTH historical + indicator buffers in memory
    before issuing any PUT. Only then write historical, then indicators.
    If either buffer computation fails, no PUT happens.
    """
    hist_key = f"parquet/historical/{timeframe}/{ticker}.parquet"
    ind_key = f"parquet/indicators/{timeframe}/{ticker}.parquet"

    try:
        # Resolve fetch window
        if mode == "full":
            start = date.today() - timedelta(days=_FULL_LOOKBACK_DAYS)
            existing = None
        else:  # incremental
            try:
                existing = read_ohlcv(io.BytesIO(r2.get_object(hist_key)))
            except R2NotFoundError:
                existing = None
            # All other errors propagate to the outer try/except → RefreshResult(succeeded=False)
            if existing is not None and len(existing) > 0:
                last_ts = existing["timestamp"].max()
                start = _next_start(last_ts, timeframe)
            else:
                start = date.today() - timedelta(days=_FULL_LOOKBACK_DAYS)

        # Fetch via the Massive adapter (A1)
        new_bars = fetch_bars(massive, ticker, timeframe=timeframe, start=start, end=date.today())

        # Normalize new_bars timestamps to UTC so dedupe_concat gets consistent tz.
        # parquet_store.read_ohlcv always returns UTC; fetch_bars returns ET-aware
        # timestamps from MassiveClient. pandas 2.x concat of mixed tz produces
        # object dtype — normalize here before the merge.
        if new_bars["timestamp"].dt.tz is not None and str(new_bars["timestamp"].dt.tz) != "UTC":
            new_bars = new_bars.copy()
            new_bars["timestamp"] = pd.to_datetime(new_bars["timestamp"], utc=True)

        # Compose the final OHLCV frame
        if existing is not None and len(existing) > 0:
            ohlcv = dedupe_concat(existing, new_bars)
        else:
            ohlcv = new_bars

        if ohlcv is None or len(ohlcv) == 0:
            return RefreshResult(ticker, timeframe, succeeded=False, error="empty OHLCV result")

        # A5: serialize both buffers BEFORE any PUT
        indicators = _compute_indicators_adapter(ohlcv)
        hist_buf = io.BytesIO()
        write_ohlcv(hist_buf, ohlcv, timeframe=timeframe)
        ind_buf = io.BytesIO()
        write_indicators(ind_buf, indicators, timeframe=timeframe)

        # Only now issue PUTs
        r2.put_object(hist_key, hist_buf.getvalue())
        r2.put_object(ind_key, ind_buf.getvalue())

        return RefreshResult(ticker, timeframe, succeeded=True, rows_written=len(ohlcv))
    except Exception as exc:  # noqa: BLE001 — convert ALL vendor/r2/parse errors to a typed failure
        return RefreshResult(
            ticker,
            timeframe,
            succeeded=False,
            error=f"{type(exc).__name__}: {exc}",
        )


def main(argv: list[str] | None = None) -> int:
    load_dotenv(_PROJECT_ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    args = build_parser().parse_args(argv)

    if args.dry_run:
        from scripts.ta_lib.dry_run_store import DryRunStore

        r2 = DryRunStore(_PROJECT_ROOT / "data" / "apex_mirror_preview")
        logger.info("Dry-run mode: writing to %s", r2.bucket)
    else:
        from scripts.ta_lib.r2_store import R2Store

        r2 = R2Store()

    # Task 7 will replace this stub with run_refresh(r2=r2, mode=..., ...)
    universe = load_universe(r2)
    targets = list(expand_targets(universe, timeframes=tuple(args.timeframes)))
    logger.info("Loaded %d tickers; %d (ticker, tf) targets", len(universe), len(targets))
    logger.info("Mode=%s dry_run=%s workers=%d", args.mode, args.dry_run, args.max_workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
