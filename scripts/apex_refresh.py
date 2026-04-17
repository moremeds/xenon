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
from zoneinfo import ZoneInfo

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
    parser.add_argument("--max-workers", type=int, default=_DEFAULT_MAX_WORKERS)
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

import numpy as np
import pandas as pd

from scripts.clients.massive_client import MassiveClient
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

    # T5: guard div-by-zero so inf never reaches the indicator parquet. When close is
    # 0.0 (pathological), the result is NaN and TAService._coerce_float will map NaN
    # -> 0.0 at the scanner boundary. inf would flow through and confuse downstream.
    safe_close = enriched["close"].where(enriched["close"] > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        range_numerator = (
            enriched["high"].rolling(20, min_periods=20).max() - enriched["low"].rolling(20, min_periods=20).min()
        )
        enriched["range_20d_pct"] = (range_numerator / safe_close).replace([np.inf, -np.inf], np.nan)
        enriched["atr_pct"] = (enriched["atr_14"] / safe_close).replace([np.inf, -np.inf], np.nan)

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

        # T2: historical first, then indicators. If indicators fails, roll back the
        # historical PUT so the ticker's two-parquet state stays consistent (both
        # present or neither). The outer try/except converts the re-raised
        # indicator exception to RefreshResult(succeeded=False).
        r2.put_object(hist_key, hist_buf.getvalue())
        try:
            r2.put_object(ind_key, ind_buf.getvalue())
        except Exception as ind_exc:
            try:
                r2.delete_object(hist_key)
                logger.warning(
                    "rolled back historical PUT for %s/%s after indicator failure: %s",
                    ticker,
                    timeframe,
                    ind_exc,
                )
            except Exception:
                logger.exception(
                    "FAILED to roll back historical PUT for %s/%s — manual cleanup may be needed",
                    ticker,
                    timeframe,
                )
            raise

        return RefreshResult(ticker, timeframe, succeeded=True, rows_written=len(ohlcv))
    except Exception as exc:  # noqa: BLE001 — convert ALL vendor/r2/parse errors to a typed failure
        return RefreshResult(
            ticker,
            timeframe,
            succeeded=False,
            error=f"{type(exc).__name__}: {exc}",
        )


import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from scripts.ta_lib.r2_store import R2PreconditionError

_FAILURE_RATIO_ABORT = 0.50
_SCHEMA_VERSION = 1
_MANIFEST_KEY = "meta/last_updated.json"
_DATA_QUALITY_KEY = "meta/data_quality.json"
_DEFAULT_MAX_WORKERS = 5  # A22: conservative vs. Massive rate limits + session contention


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _update_manifest_with_retry(r2, *, attempts: int = 3) -> None:
    """A16: CAS write of meta/last_updated.json with exponential backoff on ETag mismatch."""
    now = _now_iso()
    for attempt in range(attempts):
        try:
            prev = r2.head(_MANIFEST_KEY)
            prev_etag = prev["ETag"] if prev else None
            existing = r2.get_json(_MANIFEST_KEY) if prev else {}
            manifest = {
                **existing,
                "historical": now,
                "indicators": now,
                "schema_version": _SCHEMA_VERSION,
            }
            r2.put_json(_MANIFEST_KEY, manifest, if_match=prev_etag)
            return
        except R2PreconditionError:
            if attempt == attempts - 1:
                raise
            time.sleep(2**attempt)


def run_refresh(
    *,
    r2,
    mode: str,
    timeframes: tuple[str, ...],
    max_workers: int = _DEFAULT_MAX_WORKERS,
) -> int:
    """Orchestrator.

    Returns 0 on success (failure_ratio <= 50%), 3 when >50% of tickers failed,
    1 on other unrecoverable error (propagated to GH action status).

    Threshold is strict >: exactly 50% failure still passes (A21).

    Per A4: on degraded runs (>50% failure), the manifest is NOT updated —
    the scanner continues to see yesterday's fresh data and the next Action
    run starts from the same baseline. data_quality.json is still written as
    a diagnostic.

    Per A22: max_workers defaults to 5 (conservative vs. Massive rate limits
    and requests.Session connection-pool contention). MassiveClient is
    instantiated once and shared across threads.
    """
    universe = load_universe(r2)
    targets = list(expand_targets(universe, timeframes=timeframes))
    logger.info("Refreshing %d targets in %s mode (workers=%d)", len(targets), mode, max_workers)

    massive = MassiveClient()
    results: list[RefreshResult] = []

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [
            ex.submit(refresh_one, r2=r2, massive=massive, ticker=t, timeframe=tf, mode=mode) for t, tf in targets
        ]
        for fut in as_completed(futures):
            res = fut.result()
            results.append(res)
            if not res.succeeded:
                logger.warning("refresh failed: %s/%s — %s", res.ticker, res.timeframe, res.error)

    failed = [r for r in results if not r.succeeded]
    total = max(len(results), 1)
    failure_ratio = len(failed) / total
    logger.info(
        "Done. %d ok, %d failed (%.1f%%)",
        len(results) - len(failed),
        len(failed),
        failure_ratio * 100,
    )

    # A4: write data_quality on every run (diagnostic)
    dq = {
        "generated_at": _now_iso(),
        "mode": mode,
        "total_entries": len(results),
        "by_status": {"PASS": len(results) - len(failed), "FAIL": len(failed)},
        "failures": [{"symbol": r.ticker, "timeframe": r.timeframe, "error": r.error} for r in failed[:200]],
    }
    try:
        r2.put_json(_DATA_QUALITY_KEY, dq)
    except Exception:  # noqa: BLE001
        logger.exception("failed to write %s — continuing", _DATA_QUALITY_KEY)

    if failure_ratio > _FAILURE_RATIO_ABORT:
        logger.error("Aborting manifest update: failure_ratio=%.2f > %.2f", failure_ratio, _FAILURE_RATIO_ABORT)
        return 3

    # Only now update the manifest (A4)
    _update_manifest_with_retry(r2)
    return 0


_ET = ZoneInfo("America/New_York")
_MARKET_CLOSE_HOUR = 16  # 4 PM ET


def _prior_trading_day(now_et: datetime) -> date:
    """Return the most recent ET weekday whose close (16:00 ET) has passed."""
    candidate = now_et.date()
    # If it's a weekday but before 16:00 ET, step back a day
    if now_et.hour < _MARKET_CLOSE_HOUR and candidate.weekday() < 5:
        candidate = candidate - timedelta(days=1)
    # Skip back through weekends
    while candidate.weekday() >= 5:
        candidate = candidate - timedelta(days=1)
    return candidate


def _incremental_session_ready(r2, *, now_et: datetime | None = None) -> tuple[bool, str]:
    """A18: return (ready, reason). If not ready, caller should exit 0 and defer.

    Error handling (T1):
      MassiveNoDataError                              -> defer cleanly (vendor catches up later)
      MassiveAuthError                                -> defer loudly (config issue; no point running)
      MassiveRateLimitError / requests.RequestException -> proceed (transient; per-ticker retry in run)
      any other exception                             -> defer with reason (fail-closed on unknown state)
    """
    import requests

    from scripts.clients.massive_client import (
        MassiveAuthError,
        MassiveNoDataError,
        MassiveRateLimitError,
    )

    now_et = now_et or datetime.now(_ET)
    if now_et.weekday() < 5 and now_et.hour < _MARKET_CLOSE_HOUR:
        return False, f"pre-close ({now_et:%Y-%m-%d %H:%M ET}) — defer"

    target = _prior_trading_day(now_et)
    try:
        massive = MassiveClient()
        fetch_bars(massive, "SPY", timeframe="1d", start=target, end=target)
    except MassiveNoDataError:
        return False, f"Massive has not published SPY 1d for {target} yet — defer"
    except MassiveAuthError as exc:
        return False, f"MassiveAuthError during A18 probe: {exc} — defer"
    except (MassiveRateLimitError, requests.RequestException) as exc:
        logger.warning("A18 probe transient error: %s — proceeding (run will retry)", exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"A18 probe failed unexpectedly ({type(exc).__name__}: {exc}) — defer"
    return True, ""


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

    # A18: incremental mode defers if US session isn't complete yet
    if args.mode == "incremental":
        ready, reason = _incremental_session_ready(r2)
        if not ready:
            logger.info("A18 deferred: %s", reason)
            return 0

    return run_refresh(
        r2=r2,
        mode=args.mode,
        timeframes=tuple(args.timeframes),
        max_workers=args.max_workers,
    )


if __name__ == "__main__":
    sys.exit(main())
