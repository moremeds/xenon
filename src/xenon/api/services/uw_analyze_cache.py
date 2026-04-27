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
from collections import OrderedDict
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[4]

logger = logging.getLogger("xenon.uw_analyze_cache")

# ── Tunables ────────────────────────────────────────────────────────────────
# Default TTLs are enforced as the in-code fallback when the env vars are
# unset. `_ttl()` re-reads the env on every call, so a runtime
# ``export XENON_UW_TTL_OPEN_S=600`` takes effect immediately without
# restarting FastAPI. Daily UW budget is ~20k calls; the 30-min open TTL keeps
# the /uw-analyze portfolio under budget on a 70-ticker set.
_TTL_OPEN_S = 1800  # 30 min during market hours
_TTL_CLOSED_S = 3600  # 60 min when closed (used for user-initiated fetches only —
# automatic refresh is blocked entirely outside market hours,
# see get_or_run closed-market gate)
_MAX_PARALLEL_RUNS = 3  # global cap on concurrent UW calls
_RUN_TIMEOUT_S = 60.0  # per-ticker analyser timeout

# Memory bounds. Each cache entry holds a `current` snapshot (UW report + display)
# which is ~30-80 KB of Python dict overhead. Without a cap, a long-lived uvicorn
# process accumulates one entry per unique ticker ever analysed, plus a permanent
# asyncio.Lock per ticker. Observed RSS has surged past 7 GB. These bounds make
# the ceiling predictable.
_MAX_ENTRIES = int(os.environ.get("XENON_UW_CACHE_MAX", "300"))
# Keep at most this many materialized diff records per entry. The full diff
# history is not used — UI only reads the latest, and flow capture only needs
# the latest refresh's changes.
_MAX_MATERIALIZED_CHANGES = 10
# Eviction tiers. Lower number = evicted first. Entries tagged with any of
# these sources belong to the corresponding tier; ties broken by LRU order.
_SOURCE_TIERS: dict[str, int] = {"adhoc": 0, "watchlist": 1, "portfolio": 2}

_DEFAULT_CACHE_PATH = _PROJECT_ROOT / "data" / "uw_analyze_cache.json"
_DEFAULT_HISTORY_DIR = _PROJECT_ROOT / "data" / "uw_analyze_history"

Source = str  # "portfolio" | "watchlist" | "adhoc"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_market_open_default() -> bool:
    try:
        from xenon.utils.market_hours import is_market_open

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


# Display/derived fields where `None` in a new snapshot almost always
# means a transient UW fetch failure (rate limit, timeout, 5xx), not a
# legitimate state change. For these we prefer to keep the previous
# snapshot's value instead of clobbering good data with `None` — this
# stops the UI from flashing "—" every time the refresh cycle hits a
# 429 on a single endpoint.
#
# Fields NOT listed here (e.g. `net_call_premium`, `flow_score`, `spot`,
# `gex_sign`) are high-frequency mutable values; a stale carry-over
# would be misleading, so those still clobber even when None.
_DISPLAY_STICKY_FIELDS: frozenset[str] = frozenset(
    {
        "sector",
        "iv_rank",
        "iv",
        "rv",
        "iv_52w_low",
        "iv_52w_high",
        "gamma_per_1pct",
        "call_wall_strike",
        "put_wall_strike",
        "short_volume_ratio",
        "short_volume_trend",
        "term_structure_label",
        "max_pain",
    }
)
_DERIVED_STICKY_FIELDS: frozenset[str] = frozenset(
    {
        "call_wall",
        "put_wall",
        "max_pain",
        "iv_rank",
    }
)


def _merge_sticky_fields(new_snapshot: dict, prev_snapshot: Optional[dict]) -> None:
    """Last-known-good carry-over for enrichment fields that tend to
    transiently vanish on UW 429/5xx.

    Mutates ``new_snapshot`` in place. Only fills a field from
    ``prev_snapshot`` if the new value is ``None`` AND the old value was
    populated — legitimate value transitions (non-None -> new non-None)
    are preserved.
    """
    if not isinstance(prev_snapshot, dict):
        return
    new_display = new_snapshot.get("display")
    prev_display = prev_snapshot.get("display")
    if isinstance(new_display, dict) and isinstance(prev_display, dict):
        for field in _DISPLAY_STICKY_FIELDS:
            if new_display.get(field) is None and prev_display.get(field) is not None:
                new_display[field] = prev_display[field]
    new_derived = new_snapshot.get("derived")
    prev_derived = prev_snapshot.get("derived")
    if isinstance(new_derived, dict) and isinstance(prev_derived, dict):
        for field in _DERIVED_STICKY_FIELDS:
            if new_derived.get(field) is None and prev_derived.get(field) is not None:
                new_derived[field] = prev_derived[field]


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


def build_snapshot(
    ticker: str,
    report: dict,
    display: dict,
    flow_alerts: Optional[list[dict]] = None,
    dark_pool_summary: Optional[dict] = None,
    options_flow_summary: Optional[dict] = None,
) -> dict:
    """Construct a cache entry snapshot.

    The ``dark_pool_summary`` and ``options_flow_summary`` keys carry the
    per-ticker outputs of ``scripts/analysis/dark_pool_summary.summarize_dark_pool``
    and ``scripts/analysis/options_flow_summary.summarize_options_flow`` so
    downstream consumers (notably the /flow-analysis portfolio classifier)
    can read them without rerunning the UW API. They default to ``None``
    when the caller did not compute them so existing uw-analyze consumers
    that only read report/display/derived stay unaffected.
    """
    return {
        "ticker": ticker,
        "ts": _now_iso(),
        "report": report,
        "display": display,
        "flow_alerts": list(flow_alerts or []),
        "derived": derive_from_report(report, display),
        "dark_pool_summary": dark_pool_summary,
        "options_flow_summary": options_flow_summary,
    }


# ── Cache class ─────────────────────────────────────────────────────────────


class UwAnalyzeCache:
    """In-memory + on-disk cache with TTL, singleflight, and atomic writes."""

    def __init__(
        self,
        *,
        cache_path: Optional[Path] = None,
        history_path: Optional[Path] = None,
        market_open_fn: Callable[[], bool] = _is_market_open_default,
        ttl_open_s: int = _TTL_OPEN_S,
        ttl_closed_s: int = _TTL_CLOSED_S,
        max_parallel: int = _MAX_PARALLEL_RUNS,
        max_entries: int = _MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cache_path = Path(cache_path) if cache_path else _DEFAULT_CACHE_PATH
        # History archive: one file per materialized refresh, under
        # <history_path>/<TICKER>/YYYYMMDD-HHMMSS-ffffff.json. Default
        # lives next to cache.json under data/uw_analyze_history.
        self.history_path = Path(history_path) if history_path else _DEFAULT_HISTORY_DIR
        self._market_open_fn = market_open_fn
        self._ttl_open = ttl_open_s
        self._ttl_closed = ttl_closed_s
        self._semaphore = asyncio.Semaphore(max_parallel)
        self._clock = clock
        self._max_entries = max_entries
        # OrderedDict preserves insertion/access order for LRU eviction.
        self._entries: "OrderedDict[str, dict]" = OrderedDict()
        self._per_ticker_locks: dict[str, asyncio.Lock] = {}
        self._disk_lock = asyncio.Lock()
        self._loaded = False

    # ── Disk I/O ──────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        """Lazily rehydrate the on-disk cache into memory.

        The ``_loaded`` flag is only set to True on a *successful* parse of
        a well-formed file, OR when the file is confirmed absent. Any other
        outcome (raise, malformed shape) leaves ``_loaded`` False so the
        next access retries — a transient partial-read or a file that
        appears later must not cause permanent blindness to the archive.
        """
        if self._loaded:
            return
        if not self.cache_path.exists():
            # Legitimate cold start. Mark loaded so we don't stat the disk
            # on every call; the next write will create the file and the
            # in-memory state is authoritative from here.
            self._loaded = True
            return
        try:
            raw = self.cache_path.read_text()
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            # Transient read/parse failure (e.g., concurrent writer, zero-
            # byte file from a crashed process). Do NOT set _loaded — the
            # next access will retry and hopefully win the race.
            logger.warning(
                "uw_analyze_cache read/parse failed (%s) — will retry on next access",
                exc,
            )
            return
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            # File exists and parses but is shaped wrong (wrong schema,
            # empty `{}`, list payload from a legacy format). Leave
            # _loaded False so a hand-fix of the file is picked up
            # without restarting the process.
            logger.warning(
                "uw_analyze_cache file %s present but entries key missing or wrong shape "
                "(top-level type=%s) — will retry on next access",
                self.cache_path,
                type(data).__name__,
            )
            return
        # Rehydrate into OrderedDict in whatever order the file provides;
        # LRU order will re-establish on first access.
        self._entries = OrderedDict(entries)
        # Enforce the cap immediately on load — a pre-bounds cache file
        # may contain more entries than the new limit allows.
        self._evict_if_over_cap()
        self._loaded = True
        logger.info(
            "uw_analyze_cache loaded %d entries from %s",
            len(self._entries),
            self.cache_path,
        )

    async def _persist(self, *, allow_empty: bool = False) -> None:
        """Atomically rewrite the on-disk cache.

        Refuses to overwrite a non-trivially-sized existing file with an
        empty in-memory state unless ``allow_empty=True``. This blocks a
        subtle data-loss path: if ``_ensure_loaded`` ever failed silently
        and left ``_entries`` empty, the next write from the route layer
        would otherwise nuke the real archive. Callers that legitimately
        want to wipe the cache must pass ``allow_empty=True`` explicitly.
        """
        async with self._disk_lock:
            if not allow_empty and not self._entries and self.cache_path.exists():
                try:
                    existing_size = self.cache_path.stat().st_size
                except OSError:
                    existing_size = 0
                # 64 bytes is larger than `{"updated_at": "...", "entries": {}}`
                # but smaller than any file carrying even one entry.
                if existing_size > 64:
                    logger.error(
                        "uw_analyze_cache refusing to overwrite non-empty disk file "
                        "(%d bytes) with empty memory state — likely a failed load. "
                        "Pass allow_empty=True to force.",
                        existing_size,
                    )
                    return
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

    # ── History archive ───────────────────────────────────────────────

    def _archive_file_for(self, ticker: str) -> Path:
        """Build the archive filename for a just-committed snapshot.

        Uses microsecond precision so two back-to-back forced refreshes
        for the same ticker cannot collide within one wall-clock second.
        Filenames remain lexicographically sortable by timestamp, which
        ``load_history`` relies on to filter before parsing.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        return self.history_path / ticker.upper() / f"{stamp}.json"

    def _write_archive_sync(self, target: Path, payload: dict) -> None:
        """Sync worker for archive writes. Runs inside asyncio.to_thread."""
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(prefix=".uw_archive_", suffix=".json", dir=str(target.parent))
        try:
            with os.fdopen(tmp_fd, "w") as fh:
                json.dump(payload, fh, indent=2, default=str)
            os.replace(tmp_path, target)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    async def _archive_snapshot(
        self,
        ticker: str,
        entry: dict,
        materialized_changes: list[dict],
    ) -> None:
        """Append a just-committed snapshot to the history archive.

        MUST be called only AFTER ``_persist()`` has successfully written
        the live cache. Ordering matters: if archive lands first and
        persist then fails, history would contain a phantom snapshot
        that never became the committed ``current`` state.

        Archive schema (documented contract):
          Top-level keys — guaranteed: ``current``, ``materialized_changes``,
          ``archived_at``.
          Nested fields inside ``current`` — ``dark_pool_summary`` and
          ``options_flow_summary`` may be ``None``; ``flow_alerts`` may be
          empty; ``report``/``display`` sub-fields are runner-dependent.
          Consumers MUST NOT assume nested fields are populated.

        Error containment: failures are logged at WARNING but never raised
        — disk-full on the archive path must not break ``/uw-analyze``.
        The file write runs in ``asyncio.to_thread`` so it does not extend
        event-loop stall on the refresh hot path.

        Retention: none in v1. Operational guidance — add a janitor if the
        history directory exceeds ~500K files or if ``load_history`` p99
        latency exceeds ~50ms. At current rates (~1.5K files/day for a
        20-ticker portfolio) this is many months away.
        """
        try:
            current = entry.get("current") if isinstance(entry, dict) else None
            if not isinstance(current, dict):
                return
            payload = _coerce_jsonable(
                {
                    "current": current,
                    "materialized_changes": list(materialized_changes or []),
                    "archived_at": _now_iso(),
                }
            )
            target = self._archive_file_for(ticker)
            await asyncio.to_thread(self._write_archive_sync, target, payload)
            # Also archive to Postgres (full payload)
            await asyncio.to_thread(
                self._archive_to_postgres,
                ticker,
                current,
                payload.get("materialized_changes") if isinstance(payload, dict) else None,
                payload.get("archived_at") if isinstance(payload, dict) else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("uw_analyze_cache archive write failed for %s: %s", ticker, exc)

    @staticmethod
    def _archive_to_postgres(
        ticker: str,
        current: dict,
        materialized_changes: list | None = None,
        archived_at_iso: str | None = None,
    ) -> None:
        """Write snapshot to Postgres uw_analyze_snapshots (sync, for to_thread)."""
        try:
            url = os.environ.get("DATABASE_URL")
            if not url:
                return
            from datetime import datetime
            from decimal import Decimal

            from sqlalchemy import create_engine as _cse
            from sqlalchemy import insert

            from xenon.db.schema import uw_analyze_snapshots

            sync_url = url.replace("postgresql+asyncpg://", "postgresql+psycopg://")
            engine = _cse(sync_url)
            report = current.get("report") or {}
            scores = report.get("scores") if isinstance(report, dict) else None
            score_val = None
            if isinstance(scores, dict):
                score_val = scores.get("flow") or scores.get("composite") or scores.get("total")

            def _ts(value: str | None):
                if not value:
                    return None
                try:
                    return datetime.fromisoformat(value.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    return None

            with engine.begin() as conn:
                conn.execute(
                    insert(uw_analyze_snapshots).values(
                        ticker=ticker,
                        report=_coerce_jsonable(report) if isinstance(report, dict) else None,
                        display=_coerce_jsonable(current.get("display")),
                        derived=_coerce_jsonable(current.get("derived")),
                        dark_pool_summary=_coerce_jsonable(current.get("dark_pool_summary")),
                        options_flow_summary=_coerce_jsonable(current.get("options_flow_summary")),
                        flow_alerts=_coerce_jsonable(current.get("flow_alerts")),
                        materialized_changes=_coerce_jsonable(materialized_changes),
                        report_fetched_at=_ts(report.get("fetched_at")) if isinstance(report, dict) else None,
                        archived_at=_ts(archived_at_iso),
                        portfolio_score=Decimal(str(score_val)) if score_val is not None else None,
                    )
                )
            engine.dispose()
        except Exception as exc:  # noqa: BLE001
            logger.warning("uw_analyze_cache Postgres archive failed for %s: %s", ticker, exc)

    def load_history(
        self,
        ticker: str,
        *,
        limit: Optional[int] = None,
        since: Optional[datetime] = None,
    ) -> list[dict]:
        """Return archived snapshots for ``ticker``, most-recent first.

        Filters on filename (which carries the full UTC timestamp) BEFORE
        parsing any JSON, so cost scales with the returned count — not the
        total archive size. Used by debug tools and future time-series
        consumers; no HTTP route wires this up yet.

        Args:
          limit: max number of snapshots to return (after sort).
          since: only include snapshots with archive stamp >= this UTC
                 datetime. Naive datetimes are treated as UTC.
        """
        ticker_dir = self.history_path / ticker.upper()
        if not ticker_dir.exists():
            return []

        # List + sort by filename descending. Filename prefix
        # YYYYMMDD-HHMMSS-ffffff is lex-sortable == time-sortable.
        try:
            names = [
                e.name
                for e in os.scandir(ticker_dir)
                if e.is_file() and e.name.endswith(".json") and not e.name.startswith(".")
            ]
        except OSError as exc:
            logger.warning("load_history scandir failed for %s: %s", ticker, exc)
            return []
        names.sort(reverse=True)

        since_stamp: Optional[str] = None
        if since is not None:
            s = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
            since_stamp = s.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")

        out: list[dict] = []
        for name in names:
            if since_stamp is not None:
                # Filename stem is the full stamp; compare as strings.
                stem = name[:-5]  # drop ".json"
                if stem < since_stamp:
                    break  # names are descending — nothing older qualifies
            if limit is not None and len(out) >= limit:
                break
            try:
                with open(ticker_dir / name) as fh:
                    out.append(json.load(fh))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("load_history parse failed for %s/%s: %s", ticker, name, exc)
                continue
        return out

    # ── TTL ───────────────────────────────────────────────────────────

    @staticmethod
    def _int_env(name: str, default: int) -> int:
        """Read a positive integer env var with safe fallback.

        Returns ``default`` when the env var is unset, empty, non-numeric,
        has whitespace, or parses to a non-positive value. Also logs a
        warning the first time a malformed value is seen so operators
        notice the misconfig without crashing the /uw-analyze read path.

        We cannot crash here: `_ttl()` is called inside `_is_fresh()` on
        every `get_or_run()`, so a ValueError would 500 every request.
        """
        raw = os.environ.get(name)
        if raw is None or raw == "":
            return default
        try:
            v = int(raw)
        except (ValueError, TypeError):
            logger.warning(
                "uw_analyze_cache: ignoring malformed %s=%r, using default %d",
                name,
                raw,
                default,
            )
            return default
        if v <= 0:
            logger.warning(
                "uw_analyze_cache: ignoring non-positive %s=%d, using default %d",
                name,
                v,
                default,
            )
            return default
        return v

    def _ttl(self) -> int:
        # Env vars override the ctor defaults at call time. Tests inject
        # explicit values via ctor kwargs; runtime tuning via env vars.
        if self._market_open_fn():
            return self._int_env("XENON_UW_TTL_OPEN_S", self._ttl_open)
        return self._int_env("XENON_UW_TTL_CLOSED_S", self._ttl_closed)

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

    # ── Eviction ──────────────────────────────────────────────────────

    def _entry_tier(self, entry: dict) -> int:
        """Higher tier = more important, evicted last. Portfolio > watchlist > adhoc."""
        sources = entry.get("sources") or []
        if not sources:
            return _SOURCE_TIERS["adhoc"]
        return max((_SOURCE_TIERS.get(s, _SOURCE_TIERS["adhoc"]) for s in sources), default=0)

    def _evict_if_over_cap(self) -> None:
        """Evict from the front of the OrderedDict, skipping higher-tier entries.

        Strategy: sort candidates by (tier, LRU index) and evict lowest-tier
        LRU entries first. Portfolio tier is only touched as a last resort.
        Also sweeps orphan locks (whose ticker is no longer in `_entries`),
        which would otherwise leak when a runner raises before insert.
        """
        overflow = len(self._entries) - self._max_entries
        if overflow > 0:
            ordered_with_index = [(self._entry_tier(e), idx, t) for idx, (t, e) in enumerate(self._entries.items())]
            # Lower tier first, then LRU index within tier. Stable enough
            # because tuple compare is total.
            ordered_with_index.sort(key=lambda x: (x[0], x[1]))
            for _tier, _idx, ticker in ordered_with_index[:overflow]:
                self._entries.pop(ticker, None)

        # Orphan-lock sweep. Safe at this point because:
        #  (a) the caller holds its own lock reference on the stack, so
        #      removing it from the dict doesn't invalidate anything;
        #  (b) concurrent waiters have already resolved `_lock_for(ticker)`
        #      before awaiting — they hold direct references too.
        if len(self._per_ticker_locks) > len(self._entries):
            orphans = [t for t in self._per_ticker_locks if t not in self._entries]
            for t in orphans:
                self._per_ticker_locks.pop(t, None)

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
        runner: Callable[[str], Awaitable[tuple[dict, dict, list[dict]]]],
        force: bool = False,
        user_initiated: bool = False,
        sources: Iterable[Source] = (),
    ) -> tuple[dict, bool]:
        """Return `(entry, did_refresh)` for `ticker`, running `runner(ticker)`
        if the snapshot is missing/stale or `force=True`.

        `did_refresh` is True when a fresh runner call just materialized a
        new snapshot; callers use it to gate write-path side effects like
        flow event capture. The materialized diff is persisted on the entry
        at `materialized_changes` so GET paths never recompute.

        `user_initiated=True` bypasses the closed-market gate. Set this on
        explicit user actions (refresh button, single-ticker analyze, CLI).
        Automatic paths (portfolio auto-poll, scheduled fills) leave it False
        so they get gated outside market hours to preserve the daily UW
        budget. See plan §2 in silly-humming-tide.md.

        `runner` is an async callable returning
        `(report_dict, display_dict, flow_alerts)`. Injection makes the
        cache trivially testable without UW or the analyser.
        """
        ticker = ticker.upper()
        self._ensure_loaded()

        # Singleflight: per-ticker lock. Lock pruning happens in
        # `_evict_if_over_cap` (orphan sweep) — doing it in a finally
        # block here races with concurrent waiters resuming.
        lock = self._lock_for(ticker)
        async with lock:
            entry = self._entries.get(ticker)
            # Double-checked freshness inside the lock — a previous waiter may
            # have just refreshed.
            if (not force) and entry and self._is_fresh(entry):
                # LRU touch: move to end on any hit.
                self._entries.move_to_end(ticker)
                if sources:
                    # Only persist when the merge actually mutates. Warm
                    # /portfolio GETs previously rewrote the full cache
                    # JSON once per ticker here even when `sources` was
                    # already a subset — which, combined with 35 concurrent
                    # tickers behind `asyncio.gather`, pegged CPU and
                    # allocated ~80MB of transient JSON per request.
                    before = tuple(entry.get("sources") or ())
                    self._merge_sources(entry, sources)
                    after = tuple(entry.get("sources") or ())
                    if before != after:
                        await self._persist()
                return entry, False

            # Closed-market gate: block automatic refreshes outside market
            # hours. Must be placed AFTER the freshness check above — fresh
            # entries (including ones freshened by a recent force=True POST)
            # flow through without being tagged served_stale. Only the
            # "I would have called the analyzer" path hits the gate.
            if not user_initiated and not self._market_open_fn():
                if entry:
                    # Stale entry during closed hours — serve last-known-good.
                    result = dict(entry)  # shallow copy; do not mutate cache
                    result["served_stale"] = True
                    result["has_snapshot"] = True
                    return result, False
                # No entry at all — return a typed empty stub. UI must handle
                # has_snapshot=False as scaffold state.
                return {
                    "current": None,
                    "has_snapshot": False,
                    "served_stale": False,
                }, False

            async with self._semaphore:
                logger.info("uw_analyze_cache running analysis for %s (force=%s)", ticker, force)
                result = await asyncio.wait_for(runner(ticker), timeout=_RUN_TIMEOUT_S)

            # Runners may return a 3-tuple (legacy test runners) or a
            # 5-tuple carrying dark_pool + options_flow summaries computed
            # at analysis time so /flow-analysis can read them without a
            # second UW fetch. Backward-compatible unpacking.
            if len(result) == 5:
                report, display, flow_alerts, dark_pool_summary, options_flow_summary = result
            else:
                report, display, flow_alerts = result
                dark_pool_summary = None
                options_flow_summary = None

            new_snapshot = build_snapshot(
                ticker,
                report,
                display,
                flow_alerts=flow_alerts,
                dark_pool_summary=dark_pool_summary,
                options_flow_summary=options_flow_summary,
            )
            prev_snapshot = entry.get("current") if entry else None
            # Last-known-good: keep sticky enrichment fields from the
            # previous snapshot when the new runner returned `None`
            # (almost always due to a transient UW 429/5xx). Must run
            # BEFORE `previous_light` drops the full previous snapshot
            # — that stub strips `display` entirely.
            _merge_sticky_fields(new_snapshot, prev_snapshot)
            existing_sources = list(entry.get("sources") or []) if entry else []
            merged_sources = sorted(set(existing_sources) | set(sources))

            # Materialize the diff once at write time — the GET path reads
            # this instead of recomputing (and re-capturing flow events)
            # on every request.
            from xenon.api.services.uw_analyze_diff import compute_changes as _compute_changes

            materialized = [c.to_dict() for c in _compute_changes(prev_snapshot, new_snapshot)]
            # Bound the in-memory and on-disk growth: keep only the most
            # recent N diffs. Callers only ever read the latest refresh.
            if len(materialized) > _MAX_MATERIALIZED_CHANGES:
                materialized = materialized[-_MAX_MATERIALIZED_CHANGES:]

            # Replace the full `previous` snapshot with a light dict that
            # still satisfies the diff engine (it reads only `derived.*`)
            # and the GET path (which exposes `prev_ts`). Dropping the
            # embedded `report`/`display`/`flow_alerts` eliminates ~95%
            # of the doubled memory cost per entry.
            previous_light: Optional[dict] = None
            if isinstance(prev_snapshot, dict):
                previous_light = {
                    "ts": prev_snapshot.get("ts"),
                    "derived": prev_snapshot.get("derived"),
                }

            new_entry = {
                "current": new_snapshot,
                "previous": previous_light,
                "oi_baseline": entry.get("oi_baseline") if entry else None,
                "sources": merged_sources or list(sources),
                "materialized_changes": materialized,
            }
            self._entries[ticker] = new_entry
            self._entries.move_to_end(ticker)
            self._evict_if_over_cap()
            await self._persist()
            # Archive AFTER persist succeeds — ordering guarantees the
            # archive is always a subset of committed cache states.
            await self._archive_snapshot(ticker, new_entry, materialized)
            return new_entry, True

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
