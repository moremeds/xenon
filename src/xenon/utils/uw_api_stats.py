"""Thread-safe UW API usage stats collector.

Tracks request counts, latency, cache hits, errors, and rate limits
across all UWClient instances in the process. Singleton module-level
instance — import ``stats`` and call ``stats.record()``.

Also keeps a rolling 96-hour history of per-hour buckets (status-class
breakdown + latency sum/count) persisted to Postgres so daily stats survive
FastAPI restarts.

Thread safety: all mutations protected by threading.Lock because
UWClient._get() runs in concurrent threads via asyncio.to_thread().
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Rolling window for latency percentile calculation (bounds memory).
_LATENCY_WINDOW = 1000

# Hourly history window — 4 days.
_HISTORY_HOURS = 96

# Persistence throttle: at most one disk write this often during record().
_PERSIST_THROTTLE_SECONDS = 30.0

# History schema version (bumped if the on-disk shape changes in a
# non-forward-compat way).
_HISTORY_SCHEMA_VERSION = 1

# Default history file path — resolves to <repo>/data/uw_api_stats_history.json.
_DEFAULT_HISTORY_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "uw_api_stats_history.json"

# Regex to extract ticker from common UW endpoint paths.
# Matches: stock/{TICKER}/..., darkpool/{TICKER}, earnings/{TICKER},
# short/{TICKER}, etf/{TICKER}/...
_TICKER_PATH_RE = re.compile(r"^(?:stock|darkpool|earnings|short|etf|lit-flow)/([A-Z][A-Z0-9.]{0,9})(?:/|$)")


def _extract_ticker(endpoint: str, params: Optional[Dict[str, Any]] = None) -> str:
    """Best-effort ticker extraction from endpoint path or query params.

    Returns the ticker string or "_market" for market-wide endpoints.
    """
    m = _TICKER_PATH_RE.match(endpoint)
    if m:
        return m.group(1)
    if params:
        # flow-alerts uses ticker_symbol, short_screener uses tickers
        for key in ("ticker_symbol", "ticker", "tickers"):
            val = params.get(key)
            if val and isinstance(val, str):
                # Comma-separated list → take first
                return val.split(",")[0].strip().upper()
    return "_market"


def _normalize_endpoint(endpoint: str) -> str:
    """Normalize endpoint by replacing ticker with * for aggregate grouping.

    stock/AAPL/volatility → stock/*/volatility
    darkpool/MSFT → darkpool/*
    """
    m = _TICKER_PATH_RE.search(endpoint)
    if not m:
        return endpoint
    return endpoint[: m.start(1)] + "*" + endpoint[m.end(1) :]


def _empty_bucket() -> Dict[str, Any]:
    return {
        "requests_2xx": 0,
        "requests_4xx": 0,
        "requests_5xx": 0,
        "cached": 0,
        "sum_latency_ms": 0.0,
        "latency_count": 0,
    }


def _floor_hour_key(ts: float) -> str:
    """Floor a unix timestamp to its UTC hour key (ISO8601 with Z)."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:00:00Z")


class UWApiStats:
    """Thread-safe in-process API usage stats collector."""

    def __init__(
        self,
        *,
        history_path: Optional[Path] = None,
        now_fn: Optional[Callable[[], float]] = None,
        persist_throttle_seconds: float = _PERSIST_THROTTLE_SECONDS,
    ) -> None:
        self._lock = threading.Lock()
        self._now_fn: Callable[[], float] = now_fn or time.time
        self._started_at = self._now_fn()
        self._totals: Dict[str, int] = {
            "requests": 0,
            "success": 0,
            "cached": 0,
            "retries": 0,
            "failures": 0,
            "rate_limits": 0,
            "connection_errors": 0,
        }
        self._by_status: Dict[int, int] = {}
        self._by_ticker: Dict[str, Dict[str, int]] = {}
        self._by_endpoint_type: Dict[str, Dict[str, int]] = {}
        self._latencies: deque[float] = deque(maxlen=_LATENCY_WINDOW)

        # Hourly history bookkeeping.
        self.history_path: Path = Path(history_path) if history_path else _DEFAULT_HISTORY_PATH
        self._persist_throttle = float(persist_throttle_seconds)
        self._hourly: Dict[str, Dict[str, Any]] = {}
        self._last_write_ts: float = 0.0

        # Load any persisted history on init. Tolerates every possible
        # failure mode so the FastAPI server never fails to boot due to
        # a corrupt history file.
        self._load_history()

    # ── public API ────────────────────────────────────────────────────

    def record(
        self,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        status: int = 0,
        latency_ms: float = 0.0,
        cached: bool = False,
        retried: bool = False,
        connection_error: bool = False,
    ) -> None:
        """Record a single API request outcome."""
        ticker = _extract_ticker(endpoint, params)
        etype = _normalize_endpoint(endpoint)
        now = self._now_fn()
        hour_key = _floor_hour_key(now)

        should_write = False
        force_write = False
        with self._lock:
            self._totals["requests"] += 1

            if cached:
                self._totals["cached"] += 1
            elif connection_error:
                self._totals["connection_errors"] += 1
                self._totals["failures"] += 1
            elif status == 200:
                self._totals["success"] += 1
                self._latencies.append(latency_ms)
            elif status == 429:
                self._totals["rate_limits"] += 1
                self._totals["failures"] += 1
            elif status >= 400:
                self._totals["failures"] += 1

            if retried:
                self._totals["retries"] += 1

            if status:
                self._by_status[status] = self._by_status.get(status, 0) + 1

            # Per-ticker
            tb = self._by_ticker.setdefault(ticker, {"requests": 0, "success": 0, "cached": 0, "failures": 0})
            tb["requests"] += 1
            if cached:
                tb["cached"] += 1
            elif status == 200:
                tb["success"] += 1
            elif status >= 400 or connection_error:
                tb["failures"] += 1

            # Per-endpoint-type (normalized)
            eb = self._by_endpoint_type.setdefault(etype, {"requests": 0, "success": 0, "avg_ms": 0.0})
            eb["requests"] += 1
            if status == 200 and not cached:
                eb["success"] += 1
                # Running average
                n = eb["success"]
                eb["avg_ms"] = eb["avg_ms"] + (latency_ms - eb["avg_ms"]) / n

            # ── Hourly bucket update ─────────────────────────────────
            is_new_hour = hour_key not in self._hourly
            if is_new_hour:
                self._hourly[hour_key] = _empty_bucket()
                self._prune_history(now_ts=now)
                force_write = True  # new-hour boundary → immediate save
            bucket = self._hourly[hour_key]
            if cached:
                bucket["cached"] += 1
            elif connection_error:
                bucket["requests_5xx"] += 1
            elif status == 200:
                bucket["requests_2xx"] += 1
                bucket["sum_latency_ms"] += float(latency_ms)
                bucket["latency_count"] += 1
            elif 400 <= status < 500:
                bucket["requests_4xx"] += 1
            elif 500 <= status < 600:
                bucket["requests_5xx"] += 1

            # Throttle decision happens inside the lock so two threads
            # can't both decide "my turn to write" at the same instant.
            if force_write or (now - self._last_write_ts) >= self._persist_throttle:
                self._last_write_ts = now
                should_write = True
                snapshot = self._snapshot_history_locked()

        # Actual disk I/O happens AFTER releasing the lock to avoid
        # blocking other record() callers on filesystem latency.
        if should_write:
            self._write_history_atomic(snapshot)  # type: ignore[arg-type]

    def get_stats(self) -> Dict[str, Any]:
        """Return a snapshot of all collected stats."""
        with self._lock:
            latencies = sorted(self._latencies)
            n = len(latencies)
            lat: Dict[str, Any] = {"samples": n}
            if n > 0:
                lat["min"] = round(latencies[0], 1)
                lat["max"] = round(latencies[-1], 1)
                lat["avg"] = round(sum(latencies) / n, 1)
                p95_idx = min(int(n * 0.95), n - 1)
                lat["p95"] = round(latencies[p95_idx], 1)

            return {
                "session_started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._started_at)),
                "uptime_seconds": round(self._now_fn() - self._started_at),
                "totals": dict(self._totals),
                "latency_ms": lat,
                "by_status": dict(self._by_status),
                "by_ticker": {k: dict(v) for k, v in self._by_ticker.items()},
                "by_endpoint_type": {
                    k: {kk: round(vv, 1) if isinstance(vv, float) else vv for kk, vv in v.items()}
                    for k, v in self._by_endpoint_type.items()
                },
            }

    def get_stats_with_daily(self) -> Dict[str, Any]:
        """Return get_stats() + daily breakdown under a single lock.

        Avoids a torn snapshot where a concurrent ``record()`` between
        two separate lock acquisitions makes totals and daily inconsistent.
        """
        with self._lock:
            # --- session stats (same as get_stats but inside shared lock) ---
            latencies = sorted(self._latencies)
            n = len(latencies)
            lat: Dict[str, Any] = {"samples": n}
            if n > 0:
                lat["min"] = round(latencies[0], 1)
                lat["max"] = round(latencies[-1], 1)
                lat["avg"] = round(sum(latencies) / n, 1)
                p95_idx = min(int(n * 0.95), n - 1)
                lat["p95"] = round(latencies[p95_idx], 1)

            result: Dict[str, Any] = {
                "session_started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._started_at)),
                "uptime_seconds": round(self._now_fn() - self._started_at),
                "totals": dict(self._totals),
                "latency_ms": lat,
                "by_status": dict(self._by_status),
                "by_ticker": {k: dict(v) for k, v in self._by_ticker.items()},
                "by_endpoint_type": {
                    k: {kk: round(vv, 1) if isinstance(vv, float) else vv for kk, vv in v.items()}
                    for k, v in self._by_endpoint_type.items()
                },
            }

            # --- daily stats (same as get_daily_stats but under shared lock) ---
            hourly_snapshot = dict(self._hourly)

        # Compute daily outside the lock — only reads the snapshot.
        result["daily"] = self._compute_daily_from_hourly(hourly_snapshot)
        return result

    def _compute_daily_from_hourly(self, hourly: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Compute daily stats from an hourly snapshot. Lock-free."""
        _ET = ZoneInfo("America/New_York")
        now_utc = datetime.fromtimestamp(self._now_fn(), tz=timezone.utc)
        now_et = now_utc.astimezone(_ET)

        today_8pm_et = now_et.replace(hour=20, minute=0, second=0, microsecond=0)
        reset_et = today_8pm_et - timedelta(days=1) if now_et < today_8pm_et else today_8pm_et

        reset_utc = reset_et.astimezone(timezone.utc)
        reset_key = reset_utc.strftime("%Y-%m-%dT%H:00:00Z")
        reset_at_iso = reset_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        sum_2xx = sum_4xx = sum_5xx = sum_cached = latency_count = 0
        sum_latency = 0.0
        for key, bucket in hourly.items():
            if key < reset_key:
                continue
            sum_2xx += int(bucket.get("requests_2xx", 0))
            sum_4xx += int(bucket.get("requests_4xx", 0))
            sum_5xx += int(bucket.get("requests_5xx", 0))
            sum_cached += int(bucket.get("cached", 0))
            sum_latency += float(bucket.get("sum_latency_ms", 0.0))
            latency_count += int(bucket.get("latency_count", 0))

        requests = sum_2xx + sum_4xx + sum_5xx
        total_calls = requests + sum_cached
        return {
            "reset_at": reset_at_iso,
            "requests": requests,
            "requests_2xx": sum_2xx,
            "requests_4xx": sum_4xx,
            "requests_5xx": sum_5xx,
            "cached": sum_cached,
            "cache_hit_pct": round(sum_cached / total_calls * 100) if total_calls > 0 else 0,
            "avg_latency_ms": round(sum_latency / latency_count, 1) if latency_count > 0 else None,
        }

    def get_hourly_history(self, hours: int = _HISTORY_HOURS) -> list[Dict[str, Any]]:
        """Return a list of hour-buckets ordered ascending by hour.

        The list is always exactly ``hours`` long — missing hours are
        zero-filled. Average latency is computed on read from the stored
        sum/count pair; empty buckets return ``None``.
        """
        hours = max(1, int(hours))
        now = self._now_fn()
        end_dt = datetime.fromtimestamp(now, tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
        result: list[Dict[str, Any]] = []
        with self._lock:
            for i in range(hours - 1, -1, -1):
                key = (end_dt - timedelta(hours=i)).strftime("%Y-%m-%dT%H:00:00Z")
                b = self._hourly.get(key)
                if b is None:
                    avg_lat: Optional[float] = None
                    row = {
                        "hour": key,
                        "requests_2xx": 0,
                        "requests_4xx": 0,
                        "requests_5xx": 0,
                        "cached": 0,
                        "avg_latency_ms": avg_lat,
                    }
                else:
                    count = b.get("latency_count", 0) or 0
                    avg_lat = (b["sum_latency_ms"] / count) if count else None
                    row = {
                        "hour": key,
                        "requests_2xx": int(b.get("requests_2xx", 0)),
                        "requests_4xx": int(b.get("requests_4xx", 0)),
                        "requests_5xx": int(b.get("requests_5xx", 0)),
                        "cached": int(b.get("cached", 0)),
                        "avg_latency_ms": round(avg_lat, 1) if avg_lat is not None else None,
                    }
                result.append(row)
        return result

    def get_daily_stats(self) -> Dict[str, Any]:
        """Sum hourly buckets since the most recent 8PM ET boundary.

        UW's 20k/day quota resets at 8PM ET. This method finds the most
        recent 8PM ET in wall-clock time (DST-aware via ZoneInfo), converts
        to a UTC hour key, and sums all buckets at or after that key.

        ``requests`` excludes cached hits because the local cache doesn't
        consume UW's daily quota.
        """
        with self._lock:
            hourly_snapshot = dict(self._hourly)
        return self._compute_daily_from_hourly(hourly_snapshot)

    def reset(self) -> None:
        """Reset session counters. Preserves hourly history buckets —
        resetting those is a separate, destructive operation."""
        with self._lock:
            self._started_at = self._now_fn()
            for k in self._totals:
                self._totals[k] = 0
            self._by_status.clear()
            self._by_ticker.clear()
            self._by_endpoint_type.clear()
            self._latencies.clear()

    def clear_history(self) -> None:
        """Destructive: wipe all hourly buckets.

        Separate from reset() because hourly history is persistent-across-
        restarts data and resetting it would defeat the whole point of
        persistence. Call only when explicitly requested.
        """
        with self._lock:
            self._hourly.clear()
            self._last_write_ts = self._now_fn()
            snapshot = self._snapshot_history_locked()
        self._write_history_atomic(snapshot)

    def flush_history(self) -> None:
        """Force an immediate atomic write of the current hourly history.

        Called from the FastAPI shutdown lifespan so daily stats survive
        process restarts.
        """
        with self._lock:
            self._last_write_ts = self._now_fn()
            snapshot = self._snapshot_history_locked()
        self._write_history_atomic(snapshot)

    # ── internal: history bookkeeping ─────────────────────────────────

    def _prune_history(self, *, now_ts: float) -> None:
        """Drop hourly buckets older than the rolling window. Called under lock."""
        cutoff_dt = datetime.fromtimestamp(now_ts, tz=timezone.utc).replace(
            minute=0, second=0, microsecond=0
        ) - timedelta(hours=_HISTORY_HOURS - 1)
        cutoff_key = cutoff_dt.strftime("%Y-%m-%dT%H:00:00Z")
        stale = [k for k in self._hourly if k < cutoff_key]
        for k in stale:
            del self._hourly[k]

    def _snapshot_history_locked(self) -> Dict[str, Any]:
        """Build a JSON-serializable snapshot of the hourly buckets.

        Must be called while holding self._lock — the caller then drops
        the lock before doing disk I/O.
        """
        return {
            "updated_at": datetime.fromtimestamp(self._now_fn(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "schema_version": _HISTORY_SCHEMA_VERSION,
            "buckets": {
                k: {
                    "requests_2xx": int(v.get("requests_2xx", 0)),
                    "requests_4xx": int(v.get("requests_4xx", 0)),
                    "requests_5xx": int(v.get("requests_5xx", 0)),
                    "cached": int(v.get("cached", 0)),
                    "sum_latency_ms": float(v.get("sum_latency_ms", 0.0)),
                    "latency_count": int(v.get("latency_count", 0)),
                }
                for k, v in self._hourly.items()
            },
        }

    def _write_history_atomic(self, payload: Dict[str, Any]) -> None:
        """Persist the history payload to Postgres."""
        self._write_history_to_postgres(payload)

    def _write_history_to_postgres(self, payload: Dict[str, Any]) -> None:
        try:
            from decimal import Decimal

            from sqlalchemy.dialects.postgresql import insert as pg_insert

            from xenon.db.engine import get_sync_engine
            from xenon.db.schema import uw_api_stats as uw_stats_table

            engine = get_sync_engine()
            buckets = payload.get("buckets") or {}
            with engine.begin() as conn:
                for hour_key, b in buckets.items():
                    bucket_dt = datetime.strptime(hour_key, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    values = dict(
                        bucket_hour=bucket_dt,
                        requests=int(b.get("requests_2xx", 0))
                        + int(b.get("requests_4xx", 0))
                        + int(b.get("requests_5xx", 0)),
                        cache_hits=int(b.get("cached", 0)),
                        latency_sum=Decimal(str(b.get("sum_latency_ms", 0.0))),
                        latency_count=int(b.get("latency_count", 0)),
                        status_2xx=int(b.get("requests_2xx", 0)),
                        status_4xx=int(b.get("requests_4xx", 0)),
                        status_5xx=int(b.get("requests_5xx", 0)),
                    )
                    stmt = pg_insert(uw_stats_table).values(**values)
                    stmt = stmt.on_conflict_do_update(
                        index_elements=[uw_stats_table.c.bucket_hour],
                        set_={k: stmt.excluded[k] for k in values if k != "bucket_hour"},
                    )
                    conn.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("uw_api_stats Postgres write failed: %s", exc)

    def _load_history(self) -> None:
        """Read persisted history on startup. Tolerates every failure mode.

        After loading the hourly buckets, also rehydrates the in-memory
        session counters (``_totals``, ``_by_status``) from them so the
        sidebar shows cumulative stats after a restart instead of zeroing
        out. Latency percentile samples are NOT rebuilt — we only persist
        sum/count per hour, not the raw distribution — so ``latency_ms.p95``
        stays absent until the first new live sample arrives.
        """
        if self._load_history_from_postgres():
            return
        try:
            if not self.history_path.exists():
                return
            raw = self.history_path.read_text()
            payload = json.loads(raw)
            buckets_in = payload.get("buckets") or {}
            if not isinstance(buckets_in, dict):
                return
            loaded: Dict[str, Dict[str, Any]] = {}
            for key, val in buckets_in.items():
                if not isinstance(key, str) or not isinstance(val, dict):
                    continue
                loaded[key] = {
                    "requests_2xx": int(val.get("requests_2xx", 0) or 0),
                    "requests_4xx": int(val.get("requests_4xx", 0) or 0),
                    "requests_5xx": int(val.get("requests_5xx", 0) or 0),
                    "cached": int(val.get("cached", 0) or 0),
                    "sum_latency_ms": float(val.get("sum_latency_ms", 0.0) or 0.0),
                    "latency_count": int(val.get("latency_count", 0) or 0),
                }
            self._hourly = loaded
            # Prune immediately — the file might be from >96h ago.
            self._prune_history(now_ts=self._now_fn())
            self._rehydrate_session_counters_from_history()
            logger.info("uw_api_stats loaded %d hourly buckets from %s", len(self._hourly), self.history_path)
        except (FileNotFoundError, json.JSONDecodeError, OSError, KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "uw_api_stats history load failed (%s: %s) — starting fresh",
                type(exc).__name__,
                exc,
            )
            self._hourly = {}

    def _load_history_from_postgres(self) -> bool:
        try:
            from datetime import timedelta

            from sqlalchemy import select

            from xenon.db.engine import get_sync_engine
            from xenon.db.schema import uw_api_stats as uw_stats_table

            engine = get_sync_engine()
            cutoff = datetime.fromtimestamp(self._now_fn(), tz=timezone.utc) - timedelta(hours=96)
            with engine.connect() as conn:
                rows = conn.execute(select(uw_stats_table).where(uw_stats_table.c.bucket_hour >= cutoff)).fetchall()
            if not rows:
                return False

            loaded: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                data = row._mapping
                bucket_hour = data["bucket_hour"]
                key = bucket_hour.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
                loaded[key] = {
                    "requests_2xx": int(data.get("status_2xx") or 0),
                    "requests_4xx": int(data.get("status_4xx") or 0),
                    "requests_5xx": int(data.get("status_5xx") or 0),
                    "cached": int(data.get("cache_hits") or 0),
                    "sum_latency_ms": float(data.get("latency_sum") or 0.0),
                    "latency_count": int(data.get("latency_count") or 0),
                }
            self._hourly = loaded
            self._prune_history(now_ts=self._now_fn())
            self._rehydrate_session_counters_from_history()
            logger.info("uw_api_stats loaded %d hourly buckets from Postgres", len(self._hourly))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("uw_api_stats Postgres history load failed: %s", exc)
            return False

    def _rehydrate_session_counters_from_history(self) -> None:
        """Seed in-memory session counters from loaded hourly buckets.

        Called only from ``__init__`` via ``_load_history`` — no locking
        needed because no other thread can touch the instance yet.

        Without this, ``get_stats()`` returns zeros after a restart (the
        sidebar's "UW API", "Cache Hit", and 2xx/4xx/5xx rows zero out)
        even though ``data/uw_api_stats_history.json`` is intact on disk.
        After rehydration, live ``record()`` calls increment these same
        counters so session + historic totals merge cleanly.
        """
        total_requests = 0
        total_cached = 0
        total_success = 0
        total_failures = 0
        sum_2xx = 0
        sum_4xx = 0
        sum_5xx = 0
        for bucket in self._hourly.values():
            r2 = int(bucket.get("requests_2xx", 0))
            r4 = int(bucket.get("requests_4xx", 0))
            r5 = int(bucket.get("requests_5xx", 0))
            c = int(bucket.get("cached", 0))
            total_requests += r2 + r4 + r5 + c
            total_cached += c
            total_success += r2
            total_failures += r4 + r5
            sum_2xx += r2
            sum_4xx += r4
            sum_5xx += r5
        self._totals["requests"] = total_requests
        self._totals["cached"] = total_cached
        self._totals["success"] = total_success
        self._totals["failures"] = total_failures
        # by_status only has class granularity from history; synthesize
        # canonical entries. New live records at the same code merge in.
        if sum_2xx:
            self._by_status[200] = sum_2xx
        if sum_4xx:
            self._by_status[400] = sum_4xx
        if sum_5xx:
            self._by_status[500] = sum_5xx


# Module-level singleton — all UWClient instances share this.
stats = UWApiStats()
