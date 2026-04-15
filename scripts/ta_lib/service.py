"""TAService -- read-through cache orchestrator (Massive-backed)."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from scripts.ta_lib.bars import _today_et, fetch_bars
from scripts.ta_lib.indicators import compute_all
from scripts.ta_lib.store import (
    delete_ticker,
    get_connection,
    get_latest_bar_date,
    get_latest_bar_timestamp,
    init_schema,
    read_indicators,
    read_ohlc,
    write_indicators,
    write_ohlc,
)
from scripts.utils.market_calendar import get_last_n_trading_days

logger = logging.getLogger(__name__)

# Field name mapping: DB column -> trend_scan.py key
_FIELD_MAP = {
    "sma_20": "ma_20",
    "sma_50": "ma_50",
    "sma_200": "ma_200",
    "rsi_14": "rsi",
    "adx_14": "adx",
    "bb_width": "bbw",
}

# Split detection threshold (30%)
_SPLIT_THRESHOLD = 0.30

_ET = ZoneInfo("America/New_York")

_SUPPORTED_TIMEFRAMES = {"1d", "1h"}

# Cold-start calendar lookback per timeframe (Massive is calendar-day based).
_COLD_LOOKBACK_DAYS = {"1d": 365, "1h": 30}


@dataclass
class BulkRefreshResult:
    """Outcome of a bulk_refresh() invocation.

    Callers (e.g. ta_premarket_prep) use `failure_ratio` to decide whether to
    emit GOOD/DEGRADED/FAILED status codes.
    """

    attempted: int
    succeeded: int
    failed: int
    failed_tickers: list[str] = field(default_factory=list)
    aborted: bool = False

    @property
    def failure_ratio(self) -> float:
        return self.failed / max(self.attempted, 1)


class TAService:
    """Single entry point for TA indicator data with DuckDB caching.

    Thread safety: writes go through self._conn (main thread only via bulk_refresh).
    Reads use thread-local cursors via _read_cursor() for parallel_fetch compatibility.
    """

    def __init__(self, db_path: str = "data/ta.duckdb", massive_client=None):
        if db_path == ":memory:":
            import duckdb

            self._conn = duckdb.connect(":memory:")
        else:
            self._conn = get_connection(db_path)
        init_schema(self._conn)
        self._massive_client = massive_client
        self._local = threading.local()

    @classmethod
    def read_only(cls, conn) -> "TAService":
        """Construct a TAService bound to an existing connection.

        Skips __init__ entirely -- no Massive client, no schema init. The caller
        is responsible for passing a connection with schema already
        initialized. Use this for read-only audit paths (e.g.,
        ta_premarket_prep.classify_tickers).
        """
        svc = cls.__new__(cls)
        svc._conn = conn
        svc._massive_client = None
        return svc

    def _read_cursor(self):
        """Return a thread-local cursor for read operations."""
        if not hasattr(self._local, "cursor"):
            self._local.cursor = self._conn.cursor()
        return self._local.cursor

    def get_indicators(
        self,
        ticker: str,
        timeframe: str = "1d",
        *,
        allow_fetch: bool = True,
    ) -> pd.DataFrame:
        """Return full history DataFrame with OHLC + all indicator columns."""
        if timeframe not in _SUPPORTED_TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe '{timeframe}'. Supported: {_SUPPORTED_TIMEFRAMES}")
        ticker = ticker.upper()
        cursor = self._read_cursor()

        if not self._is_stale(ticker, timeframe, cursor):
            result = self._read_joined(ticker, timeframe, cursor)
            if result is not None and not result.empty:
                return result

        if not allow_fetch:
            raise RuntimeError(
                f"Cache miss for {ticker}/{timeframe} and allow_fetch=False. "
                "Run bulk_refresh() on the main thread first."
            )

        self._refresh(ticker, timeframe)
        return self._read_joined(ticker, timeframe, cursor)

    def get_snapshot(
        self,
        ticker: str,
        timeframe: str = "1d",
        *,
        allow_fetch: bool = True,
    ) -> dict:
        """Return latest-row dict matching the shape trend_scan.py expects."""
        ticker = ticker.upper()
        df = self.get_indicators(ticker, timeframe, allow_fetch=allow_fetch)

        if df.empty or len(df) < 1:
            raise RuntimeError(f"No indicator data for {ticker}")

        latest = df.iloc[-1]
        close = float(latest["close"])

        # Map DB column names -> trend_scan.py field names
        snapshot = {"ticker": ticker}
        snapshot["close"] = close
        snapshot["price"] = close

        for db_col, ts_key in _FIELD_MAP.items():
            val = latest.get(db_col)
            snapshot[ts_key] = 0.0 if pd.isna(val) else float(val)

        # Pass-through fields (same name in DB and trend_scan)
        for col in ("macd", "macd_signal", "macd_histogram"):
            val = latest.get(col)
            snapshot[col] = 0.0 if pd.isna(val) else float(val)

        # Derived fields from full DataFrame
        snapshot["atr_pct"] = float(latest["atr_14"]) / max(close, 1.0) if not pd.isna(latest.get("atr_14")) else 0.0

        sma_20_series = df["sma_20"].dropna().tail(5).tolist()
        snapshot["ma_20_series"] = [float(v) for v in sma_20_series]

        volumes = df["volume"].fillna(0)
        snapshot["recent_avg_volume"] = float(volumes.tail(5).mean()) if len(volumes) >= 5 else 0.0
        snapshot["avg_20d_volume"] = (
            float(volumes.tail(20).mean()) if len(volumes) >= 20 else snapshot["recent_avg_volume"]
        )

        delta = df["close"].diff().dropna()
        recent_delta = delta.tail(10)
        snapshot["recent_up_ratio"] = float((recent_delta > 0).mean()) if len(recent_delta) > 0 else 0.5

        highs = df["high"]
        lows = df["low"]
        snapshot["high_52w"] = float(highs.tail(252).max()) if not highs.empty else close

        if len(highs) >= 20:
            range_20d = float(highs.tail(20).max()) - float(lows.tail(20).min())
            snapshot["range_20d_pct"] = range_20d / max(close, 1.0)
        else:
            snapshot["range_20d_pct"] = 0.0

        # 20-day high/low and 52w low -- for breakout/breakdown detection.
        snapshot["high_20d"] = float(highs.tail(20).max()) if len(highs) >= 20 else close
        snapshot["low_20d"] = float(lows.tail(20).min()) if len(lows) >= 20 else close
        snapshot["low_52w"] = float(lows.tail(252).min()) if not lows.empty else close

        # Up-day / down-day volume ratio over last 10 sessions.
        # Require minimum 3 samples on BOTH sides -- otherwise return neutral (1.0).
        # A previously proposed 2.0 sentinel for all-up windows was dropped after
        # tribunal review: it created false-precision spikes that dominated the
        # trend score (which weights this 2x) without real directional evidence.
        recent = df.tail(10)
        if len(recent) >= 5:
            diffs = recent["close"].diff().dropna()
            vols = recent["volume"].fillna(0).loc[diffs.index]
            up_mask = diffs > 0
            down_mask = diffs < 0
            up_count = int(up_mask.sum())
            down_count = int(down_mask.sum())
            if up_count >= 3 and down_count >= 3:
                up_vol = float(vols[up_mask].mean())
                down_vol = float(vols[down_mask].mean())
                snapshot["up_day_volume_ratio"] = up_vol / max(down_vol, 1.0)
            else:
                snapshot["up_day_volume_ratio"] = 1.0
        else:
            snapshot["up_day_volume_ratio"] = 1.0

        snapshot["dollar_volume"] = close * snapshot["avg_20d_volume"]

        return snapshot

    def bulk_refresh(
        self,
        tickers: list[str],
        timeframe: str | None = None,
    ) -> BulkRefreshResult:
        """Pre-fetch OHLC + indicators for stale tickers via Massive.

        If `timeframe is None`, loops both 1d and 1h. No IB pacing anymore --
        Massive is an HTTP REST API with its own rate-limit headers handled
        by MassiveClient.

        Must be called on the main thread (shared DuckDB connection).
        """
        if self._massive_client is None:
            logger.warning("bulk_refresh: no Massive client -- using cached data only")
            return BulkRefreshResult(attempted=0, succeeded=0, failed=0)

        timeframes = ["1d", "1h"] if timeframe is None else [timeframe]
        attempted = 0
        succeeded = 0
        failed_set: set[str] = set()

        for tf in timeframes:
            stale = [t.upper() for t in tickers if self._is_stale(t.upper(), tf)]
            if not stale:
                logger.info("bulk_refresh: all %d tickers current for %s", len(tickers), tf)
                continue
            logger.info(
                "bulk_refresh: %d/%d tickers need refresh for %s",
                len(stale),
                len(tickers),
                tf,
            )

            for ticker in stale:
                attempted += 1
                try:
                    self._refresh(ticker, tf)
                    succeeded += 1
                except Exception as exc:
                    logger.warning("bulk_refresh: failed %s %s: %s", ticker, tf, exc)
                    failed_set.add(ticker)

        failed = attempted - succeeded
        return BulkRefreshResult(
            attempted=attempted,
            succeeded=succeeded,
            failed=failed,
            failed_tickers=sorted(failed_set),
            aborted=False,
        )

    def _compute_lookback_days(self, ticker: str, timeframe: str) -> int:
        """Calendar-day lookback for Massive.

        Cold start -> _COLD_LOOKBACK_DAYS[timeframe]. Incremental -> days since
        latest cached bar (minimum 1).
        """
        latest = get_latest_bar_date(self._conn, ticker, timeframe)
        if latest is None:
            return _COLD_LOOKBACK_DAYS[timeframe]
        days_behind = (_today_et() - latest).days
        return max(days_behind, 1)

    def _is_stale(self, ticker: str, timeframe: str, cursor=None) -> bool:
        """Check if cached data needs refresh."""
        conn = cursor or self._conn
        latest = get_latest_bar_date(conn, ticker, timeframe)
        if latest is None:
            return True

        # Check if indicators also exist (partial cache = stale)
        indicators = read_indicators(conn, ticker, timeframe)
        if indicators is None or len(indicators) == 0:
            return True

        if timeframe == "1h":
            # "Latest cached bar must be at or after the prior trading day's
            # 15:30 ET close" (the last full hourly bar of a session).
            prior_day_strs = get_last_n_trading_days(2)
            if not prior_day_strs:
                return True
            target_str = prior_day_strs[-2] if len(prior_day_strs) >= 2 else prior_day_strs[0]
            prior_trading_day = datetime.strptime(target_str, "%Y-%m-%d").date()
            required_min = datetime.combine(prior_trading_day, time(15, 30), tzinfo=_ET)
            latest_ts = get_latest_bar_timestamp(conn, ticker, timeframe)
            if latest_ts is None:
                return True
            # Normalize tz: compare both aware in ET.
            if latest_ts.tzinfo is None:
                latest_ts = latest_ts.replace(tzinfo=_ET)
            else:
                latest_ts = latest_ts.astimezone(_ET)
            return latest_ts < required_min

        # 1d branch -- unchanged semantics.
        now_et = datetime.now(_ET)
        last_session_str = get_last_n_trading_days(1, from_date=now_et)
        if not last_session_str:
            return True
        last_session = datetime.strptime(last_session_str[0], "%Y-%m-%d").date()
        return latest < last_session

    def _refresh(self, ticker: str, timeframe: str) -> None:
        """Fetch from Massive, compute indicators, write to DB."""
        lookback_days = self._compute_lookback_days(ticker, timeframe)
        latest = get_latest_bar_date(self._conn, ticker, timeframe)
        end_date = ""

        if latest is None:
            logger.info(
                "Cold start fetch for %s (%s, %d calendar days)",
                ticker,
                timeframe,
                lookback_days,
            )
        else:
            logger.info(
                "Incremental fetch for %s (%s, %d calendar days behind)",
                ticker,
                timeframe,
                lookback_days,
            )

        df = fetch_bars(
            self._massive_client,
            ticker,
            timeframe=timeframe,
            lookback_days=lookback_days,
            overlap_days=5,
            end_date=end_date,
        )

        # Stock split detection: compare last cached close against the first
        # truly NEW bar (after the latest cached date).
        force_full_refetch = False
        if latest is not None and len(df) > 0:
            cached_ohlc = read_ohlc(self._conn, ticker, timeframe)
            if cached_ohlc is not None and len(cached_ohlc) > 0:
                last_cached_close = float(cached_ohlc["close"].iloc[-1])
                # Filter to bars strictly after the latest cached date
                latest_ts = pd.Timestamp(latest)
                # Align tz if df["date"] is tz-aware
                df_dates = df["date"]
                if hasattr(df_dates.dtype, "tz") and df_dates.dtype.tz is not None and latest_ts.tzinfo is None:
                    latest_ts = latest_ts.tz_localize(_ET)
                new_bars = df[df["date"] > latest_ts]
                if len(new_bars) == 0:
                    new_bars = df  # fallback: all bars are "new" (cold start edge)
                first_new_open = float(new_bars["open"].iloc[0])
                if last_cached_close > 0:
                    gap = abs(first_new_open - last_cached_close) / last_cached_close
                    if gap > _SPLIT_THRESHOLD:
                        logger.warning(
                            "Split detected for %s (gap=%.1f%%), purging and re-fetching",
                            ticker,
                            gap * 100,
                        )
                        force_full_refetch = True
                        df = fetch_bars(
                            self._massive_client,
                            ticker,
                            timeframe=timeframe,
                            lookback_days=_COLD_LOOKBACK_DAYS[timeframe],
                            overlap_days=5,
                            end_date=end_date,
                        )

        # Atomic write: OHLC + indicators in one transaction
        self._conn.begin()
        try:
            if force_full_refetch:
                delete_ticker(self._conn, ticker, timeframe)

            write_ohlc(self._conn, ticker, timeframe, df)

            # Read full OHLC back (includes previously cached bars)
            full_ohlc = read_ohlc(self._conn, ticker, timeframe)
            full_ohlc_for_compute = full_ohlc.rename(columns={"bar_date": "date"})

            # Compute indicators over full series
            indicators_df = compute_all(full_ohlc_for_compute)
            indicators_df["bar_date"] = indicators_df["date"]

            write_indicators(self._conn, ticker, timeframe, indicators_df)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _read_joined(self, ticker: str, timeframe: str, cursor=None) -> pd.DataFrame:
        """Read OHLC + indicators joined by bar_date."""
        conn = cursor or self._conn
        ohlc = read_ohlc(conn, ticker, timeframe)
        indicators = read_indicators(conn, ticker, timeframe)

        if ohlc is None:
            return pd.DataFrame()

        if indicators is None:
            return pd.DataFrame()

        merged = ohlc.merge(
            indicators.drop(columns=["ticker", "timeframe", "computed_at"], errors="ignore"),
            on="bar_date",
            how="left",
        )
        merged = merged.rename(columns={"bar_date": "date"})
        return merged
