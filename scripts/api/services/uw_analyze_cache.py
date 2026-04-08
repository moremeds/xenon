"""UwAnalyzeCache — TTL'd, atomically-persisted UW analysis snapshots.

Backs the /uw-analyze/portfolio route. Concurrency model:

- Per-ticker `asyncio.Lock` (singleflight): concurrent get_or_run for the
  same ticker collapse to one upstream call.
- Global `asyncio.Semaphore(_MAX_PARALLEL_RUNS)` caps total in-flight UW
  calls across all tickers.
- Single `asyncio.Lock` guards disk writes; tmpfile + os.replace makes
  reads atomic.

Spec: docs/superpowers/specs/2026-04-08-uw-analyze-overhaul-design.md §Architecture
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

# Make scripts/ importable when running tests directly.
_SCRIPTS = Path(__file__).resolve().parents[2]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

logger = logging.getLogger("xenon.uw_analyze_cache")

# ── Tunables ────────────────────────────────────────────────────────────────
_TTL_OPEN_S = 300  # 5 min during market hours
_TTL_CLOSED_S = 1800  # 30 min when closed
_MAX_PARALLEL_RUNS = 3  # global cap on concurrent UW calls
_RUN_TIMEOUT_S = 60.0  # per-ticker analyser timeout

_DEFAULT_CACHE_PATH = _SCRIPTS.parent / "data" / "uw_analyze_cache.json"

Source = str  # "portfolio" | "watchlist" | "adhoc"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_market_open_default() -> bool:
    try:
        from utils.market_hours import is_market_open

        return bool(is_market_open())
    except Exception:  # noqa: BLE001
        return False


def _coerce_jsonable(o: Any) -> Any:
    if is_dataclass(o):
        return _coerce_jsonable(asdict(o))
    if isinstance(o, dict):
        return {k: _coerce_jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_coerce_jsonable(x) for x in o]
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return o


# ── Snapshot derivation ─────────────────────────────────────────────────────


def derive_from_report(report: dict, display: dict) -> dict:
    """Pull the diff-relevant fields out of a report+display pair into the
    flat `derived` block consumed by the diff engine.
    """
    regime = report.get("regime") if isinstance(report, dict) else None
    if not isinstance(regime, dict):
        regime = {}
    gex_sign = regime.get("gex_sign")
    if gex_sign:
        gex_sign = str(gex_sign).upper()
    flip_strike = display.get("gex_flip") if isinstance(display, dict) else None
    return {
        "gex_sign": gex_sign if gex_sign in ("POSITIVE", "NEGATIVE", "NEUTRAL") else None,
        "gex_flip_strike": flip_strike,
        "max_pain": display.get("max_pain"),
        "call_wall": display.get("call_wall_strike"),
        "put_wall": display.get("put_wall_strike"),
        "iv_rank": display.get("iv_rank"),
        "net_call_premium": display.get("net_call_premium"),
        "net_put_premium": display.get("net_put_premium"),
        "flow_score": (report.get("scores") or {}).get("flow") if isinstance(report.get("scores"), dict) else None,
        "spot": report.get("price"),
    }


def build_snapshot(ticker: str, report: dict, display: dict) -> dict:
    return {
        "ticker": ticker,
        "ts": _now_iso(),
        "report": report,
        "display": display,
        "derived": derive_from_report(report, display),
    }


# ── Cache class ─────────────────────────────────────────────────────────────


class UwAnalyzeCache:
    """In-memory + on-disk cache with TTL, singleflight, and atomic writes."""

    def __init__(
        self,
        *,
        cache_path: Optional[Path] = None,
        market_open_fn: Callable[[], bool] = _is_market_open_default,
        ttl_open_s: int = _TTL_OPEN_S,
        ttl_closed_s: int = _TTL_CLOSED_S,
        max_parallel: int = _MAX_PARALLEL_RUNS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cache_path = Path(cache_path) if cache_path else _DEFAULT_CACHE_PATH
        self._market_open_fn = market_open_fn
        self._ttl_open = ttl_open_s
        self._ttl_closed = ttl_closed_s
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._clock = clock
        self._entries: dict[str, dict] = {}
        self._per_ticker_locks: dict[str, asyncio.Lock] = {}
        self._disk_lock = asyncio.Lock()
        self._loaded = False

    # ── Disk I/O ──────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.cache_path.exists():
            return
        try:
            data = json.loads(self.cache_path.read_text())
            entries = data.get("entries") if isinstance(data, dict) else None
            if isinstance(entries, dict):
                self._entries = entries
                logger.info("uw_analyze_cache loaded %d entries from %s", len(entries), self.cache_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("uw_analyze_cache load failed (%s) — starting empty", exc)
            self._entries = {}

    async def _persist(self) -> None:
        async with self._disk_lock:
            payload = {
                "updated_at": _now_iso(),
                "entries": _coerce_jsonable(self._entries),
            }
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_fd, tmp_path = tempfile.mkstemp(
                prefix=".uw_analyze_cache_", suffix=".json", dir=str(self.cache_path.parent)
            )
            try:
                with os.fdopen(tmp_fd, "w") as fh:
                    json.dump(payload, fh, indent=2, default=str)
                os.replace(tmp_path, self.cache_path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

    # ── TTL ───────────────────────────────────────────────────────────

    def _ttl(self) -> int:
        return self._ttl_open if self._market_open_fn() else self._ttl_closed

    def _is_fresh(self, entry: dict) -> bool:
        cur = entry.get("current") if isinstance(entry, dict) else None
        if not isinstance(cur, dict):
            return False
        ts_iso = cur.get("ts")
        if not isinstance(ts_iso, str):
            return False
        try:
            ts = datetime.fromisoformat(ts_iso)
        except ValueError:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age < self._ttl()

    def _lock_for(self, ticker: str) -> asyncio.Lock:
        lk = self._per_ticker_locks.get(ticker)
        if lk is None:
            lk = asyncio.Lock()
            self._per_ticker_locks[ticker] = lk
        return lk

    # ── Public API ────────────────────────────────────────────────────

    def get_entry(self, ticker: str) -> Optional[dict]:
        self._ensure_loaded()
        return self._entries.get(ticker.upper())

    def all_entries(self) -> dict[str, dict]:
        self._ensure_loaded()
        return dict(self._entries)

    async def get_or_run(
        self,
        ticker: str,
        *,
        runner: Callable[[str], Awaitable[tuple[dict, dict]]],
        force: bool = False,
        sources: Iterable[Source] = (),
    ) -> dict:
        """Return the cached entry for `ticker`, running `runner(ticker)` if
        the snapshot is missing/stale or `force=True`.

        `runner` is an async callable returning `(report_dict, display_dict)`.
        Injection makes the cache trivially testable without UW or the
        analyser.
        """
        ticker = ticker.upper()
        self._ensure_loaded()

        # Singleflight: per-ticker lock
        async with self._lock_for(ticker):
            entry = self._entries.get(ticker)
            # Double-checked freshness inside the lock — a previous waiter may
            # have just refreshed.
            if (not force) and entry and self._is_fresh(entry):
                if sources:
                    self._merge_sources(entry, sources)
                    await self._persist()
                return entry

            async with self._semaphore:
                logger.info("uw_analyze_cache running analysis for %s (force=%s)", ticker, force)
                report, display = await asyncio.wait_for(runner(ticker), timeout=_RUN_TIMEOUT_S)

            new_snapshot = build_snapshot(ticker, report, display)
            prev_snapshot = entry.get("current") if entry else None
            existing_sources = list(entry.get("sources") or []) if entry else []
            merged_sources = sorted(set(existing_sources) | set(sources))
            new_entry = {
                "current": new_snapshot,
                "previous": prev_snapshot,
                "oi_baseline": entry.get("oi_baseline") if entry else None,
                "sources": merged_sources or list(sources),
            }
            self._entries[ticker] = new_entry
            await self._persist()
            return new_entry

    def _merge_sources(self, entry: dict, sources: Iterable[Source]) -> None:
        existing = set(entry.get("sources") or [])
        merged = sorted(existing | set(sources))
        entry["sources"] = merged

    async def upsert_sources(self, ticker: str, sources: Iterable[Source]) -> None:
        """Tag an existing entry with additional sources without re-running."""
        ticker = ticker.upper()
        self._ensure_loaded()
        entry = self._entries.get(ticker)
        if not entry:
            return
        self._merge_sources(entry, sources)
        await self._persist()
