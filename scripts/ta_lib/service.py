"""TAService — read-through cache orchestrator."""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from scripts.ta_lib.bars import fetch_bars
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

# Field name mapping: DB column → trend_scan.py key
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

# IB bar_size and default cold-start duration per timeframe
_TIMEFRAME_IB = {
    "1d": {"bar_size": "1 day", "cold_duration": "1 Y"},
    "1h": {"bar_size": "1 hour", "cold_duration": "1 M"},
}


class TAService:
    """Single entry point for TA indicator data with DuckDB caching.

    Thread safety: writes go through self._conn (main thread only via bulk_refresh).
    Reads use thread-local cursors via _read_cursor() for parallel_fetch compatibility.
    """

    def __init__(self, db_path: str = "data/ta.duckdb", ib_client=None):
        if db_path == ":memory:":
            import duckdb

            self._conn = duckdb.connect(":memory:")
        else:
            self._conn = get_connection(db_path)
        init_schema(self._conn)
        self._ib_client = ib_client
        self._local = threading.local()

    @classmethod
    def read_only(cls, conn) -> "TAService":
        """Construct a TAService bound to an existing connection.

        Skips __init__ entirely — no IB client, no schema init. The caller
        is responsible for passing a connection with schema already
        initialized. Use this for read-only audit paths (e.g.,
        ta_premarket_prep.classify_tickers).
        """
        svc = cls.__new__(cls)
        svc._conn = conn
        svc._ib_client = None
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

        # Map DB column names → trend_scan.py field names
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

        # 20-day high/low and 52w low — for breakout/breakdown detection.
        snapshot["high_20d"] = float(highs.tail(20).max()) if len(highs) >= 20 else close
        snapshot["low_20d"] = float(lows.tail(20).min()) if len(lows) >= 20 else close
        snapshot["low_52w"] = float(lows.tail(252).min()) if not lows.empty else close

        # Up-day / down-day volume ratio over last 10 sessions.
        # Require minimum 3 samples on BOTH sides — otherwise return neutral (1.0).
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

    def bulk_refresh(self, tickers: list[str], timeframe: str = "1d") -> None:
        """Pre-fetch OHLC for all stale tickers with IB pacing.

        Must be called on the main thread (ib_insync is not thread-safe).
        """
        if self._ib_client is None:
            logger.warning("bulk_refresh: no IB client — using cached data only")
            return

        stale = [t.upper() for t in tickers if self._is_stale(t.upper(), timeframe)]
        if not stale:
            logger.info("bulk_refresh: all %d tickers are current", len(tickers))
            return

        logger.info("bulk_refresh: %d/%d tickers need refresh", len(stale), len(tickers))
        batch_size = 55
        consecutive_batch_failures = 0
        backoff_s = 10

        for batch_start in range(0, len(stale), batch_size):
            if consecutive_batch_failures >= 3:
                logger.error("bulk_refresh: 3 consecutive batch failures, aborting")
                break

            batch = stale[batch_start : batch_start + batch_size]
            batch_had_failure = False
            consecutive_ticker_failures = 0

            for ticker in batch:
                try:
                    self._refresh(ticker, timeframe)
                    consecutive_ticker_failures = 0
                    backoff_s = 10
                except Exception as exc:
                    logger.warning("bulk_refresh: failed to refresh %s: %s", ticker, exc)
                    consecutive_ticker_failures += 1
                    batch_had_failure = True

                    if "pacing" in str(exc).lower() or "162" in str(exc):
                        logger.info("Pacing error — backing off %ds", backoff_s)
                        self._ib_sleep(backoff_s)
                        backoff_s = min(backoff_s * 2, 120)

                    if consecutive_ticker_failures >= 5:
                        logger.error("bulk_refresh: 5 consecutive ticker failures, skipping rest of batch")
                        break
                self._ib_sleep(0.2)

            if batch_had_failure:
                consecutive_batch_failures += 1
            else:
                consecutive_batch_failures = 0

            # Sleep between batches (skip after last batch)
            if batch_start + batch_size < len(stale):
                logger.info("bulk_refresh: pacing — sleeping 10 min before next batch")
                self._ib_sleep(600)

    def _ib_sleep(self, seconds: float) -> None:
        """Sleep without blocking ib_insync's asyncio event loop."""
        if self._ib_client and hasattr(self._ib_client, "_ib"):
            self._ib_client._ib.sleep(seconds)
        else:
            time.sleep(seconds)

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

        # Use ET-aware datetime for correct session detection
        now_et = datetime.now(_ET)
        last_session_str = get_last_n_trading_days(1, from_date=now_et)
        if not last_session_str:
            return True

        last_session = datetime.strptime(last_session_str[0], "%Y-%m-%d").date()

        if timeframe == "1d":
            return latest < last_session

        # Intraday: stale if latest bar is from a previous session, OR if
        # market is open and latest bar is more than 2 hours old
        if latest < last_session:
            return True
        market_open = now_et.hour >= 9 and (now_et.hour > 9 or now_et.minute >= 30)
        market_close = now_et.hour >= 16
        if last_session == now_et.date() and market_open and not market_close:
            # During market hours: stale if latest bar is >2h behind.
            # Use the full timestamp (not just date) so we don't treat a
            # bar from 15:00 today as if it were midnight.
            latest_ts = get_latest_bar_timestamp(conn, ticker, timeframe)
            if latest_ts is None:
                return True
            # Normalize both sides to naive for robust subtraction
            # (now_et may be tz-aware or mocked-naive; latest_ts from DuckDB
            # may be tz-aware UTC or naive).
            now_naive = now_et.replace(tzinfo=None) if now_et.tzinfo else now_et
            latest_naive = latest_ts.replace(tzinfo=None) if latest_ts.tzinfo else latest_ts
            hours_behind = (now_naive - latest_naive).total_seconds() / 3600
            return hours_behind > 2
        return False

    def _refresh(self, ticker: str, timeframe: str) -> None:
        """Fetch from IB, compute indicators, write to DB."""
        ib_cfg = _TIMEFRAME_IB[timeframe]
        bar_size = ib_cfg["bar_size"]
        cold_duration = ib_cfg["cold_duration"]
        latest = get_latest_bar_date(self._conn, ticker, timeframe)

        if latest is None:
            duration = cold_duration
            end_date = ""
            logger.info("Cold start fetch for %s (%s, %s)", ticker, duration, bar_size)
        else:
            days_behind = (date.today() - latest).days
            duration = f"{max(days_behind + 5, 5)} D"
            end_date = ""
            logger.info("Incremental fetch for %s (%s, %d days behind)", ticker, duration, days_behind)

        df = fetch_bars(self._ib_client, ticker, duration=duration, bar_size=bar_size, end_date=end_date)

        # Stock split detection: compare last cached close against the first
        # truly NEW bar (after the latest cached date). Incremental fetches include
        # overlap bars due to the +5 day buffer — comparing iloc[0] would compare
        # against an old overlapping bar and miss the split.
        force_full_refetch = False
        if latest is not None and len(df) > 0:
            cached_ohlc = read_ohlc(self._conn, ticker, timeframe)
            if cached_ohlc is not None and len(cached_ohlc) > 0:
                last_cached_close = float(cached_ohlc["close"].iloc[-1])
                # Filter to bars strictly after the latest cached date
                latest_ts = pd.Timestamp(latest)
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
                        df = fetch_bars(self._ib_client, ticker, duration=cold_duration, bar_size=bar_size)

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
