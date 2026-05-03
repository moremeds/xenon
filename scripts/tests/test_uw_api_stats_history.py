"""Tests for Phase 3 hourly history features of UWApiStats.

Covers:
- Hour-bucket categorization (2xx/4xx/5xx/cached)
- Hour rollover boundary (xx:59:59.999 vs xx+1:00:00.000)
- Rolling 96h prune on new-hour creation
- Explicit zero-fill in get_hourly_history()
- Latency sum+count storage, avg computed on read
- Disk persist/load round-trip with atomic writes
- Throttled persistence (30s min interval) under concurrency
- New-hour-boundary force-save bypasses the throttle
- Load-on-init tolerance (missing file, corrupt JSON, unknown keys)
- reset() preserves history; clear_history() wipes it
- Thread-safety of bucket updates
- Daily stats since 8PM ET boundary (DST-aware)
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from xenon.utils.uw_api_stats import UWApiStats

# ── helpers ───────────────────────────────────────────────────────────


class Clock:
    """Deterministic clock for tests. Returns seconds since epoch (float)."""

    def __init__(self, start: datetime):
        assert start.tzinfo is not None, "start must be tz-aware"
        self._t = start.timestamp()

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _hour_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:00:00Z")


def _make_stats(tmp_path, clock: Clock) -> UWApiStats:
    """Fresh stats instance with injected clock — isolation comes from the
    autouse PG truncation in conftest, not from a temp JSON file.
    """
    return UWApiStats(
        now_fn=clock.now,
        persist_throttle_seconds=30.0,
    )


def _pg_bucket(hour: str) -> dict | None:
    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql+psycopg://")
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            row = (
                conn.execute(
                    text(
                        "SELECT status_2xx, status_4xx, status_5xx, cache_hits, latency_sum, latency_count "
                        "FROM xenon.uw_api_stats WHERE bucket_hour = :bucket_hour"
                    ),
                    {"bucket_hour": hour},
                )
                .mappings()
                .fetchone()
            )
            return dict(row) if row else None
    finally:
        engine.dispose()


# ── bucket categorization ────────────────────────────────────────────


class TestBucketCategorization:
    def test_200_increments_2xx(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        hist = s.get_hourly_history(hours=1)
        assert len(hist) == 1
        row = hist[0]
        assert row["hour"] == "2026-04-10T14:00:00Z"
        assert row["requests_2xx"] == 1
        assert row["requests_4xx"] == 0
        assert row["requests_5xx"] == 0
        assert row["cached"] == 0

    def test_404_increments_4xx(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=404, latency_ms=50.0)
        row = s.get_hourly_history(hours=1)[0]
        assert row["requests_4xx"] == 1
        assert row["requests_2xx"] == 0

    def test_429_increments_4xx_and_rate_limits(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=429, latency_ms=50.0)
        row = s.get_hourly_history(hours=1)[0]
        assert row["requests_4xx"] == 1
        assert s.get_stats()["totals"]["rate_limits"] == 1

    def test_500_increments_5xx(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=503, latency_ms=200.0)
        row = s.get_hourly_history(hours=1)[0]
        assert row["requests_5xx"] == 1

    def test_connection_error_bucketed_as_5xx(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", connection_error=True)
        row = s.get_hourly_history(hours=1)[0]
        assert row["requests_5xx"] == 1

    def test_cached_hit_increments_cached_not_2xx(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, cached=True)
        row = s.get_hourly_history(hours=1)[0]
        assert row["cached"] == 1
        assert row["requests_2xx"] == 0


# ── latency: sum+count, avg on read ──────────────────────────────────


class TestLatencyOnRead:
    def test_avg_latency_computed_on_read(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        for lat in (100.0, 200.0, 300.0):
            s.record("stock/AAPL/volatility", status=200, latency_ms=lat)
        row = s.get_hourly_history(hours=1)[0]
        assert row["avg_latency_ms"] == pytest.approx(200.0)

    def test_cached_does_not_pollute_latency_avg(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        s.record("stock/AAPL/volatility", status=200, cached=True, latency_ms=0.5)
        row = s.get_hourly_history(hours=1)[0]
        assert row["avg_latency_ms"] == pytest.approx(100.0)

    def test_empty_bucket_avg_latency_is_none(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        # No records at all.
        row = s.get_hourly_history(hours=1)[0]
        assert row["avg_latency_ms"] is None


# ── hour rollover boundary ───────────────────────────────────────────


class TestHourBoundary:
    def test_5959999_lands_in_same_hour(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 59, 59, 999_000, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        hist = s.get_hourly_history(hours=1)
        assert hist[0]["hour"] == "2026-04-10T14:00:00Z"
        assert hist[0]["requests_2xx"] == 1

    def test_000000_lands_in_next_hour(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 59, 59, 999_000, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        clock.advance(0.001)  # → 15:00:00.000
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        hist = s.get_hourly_history(hours=2)
        assert hist[0]["hour"] == "2026-04-10T14:00:00Z"
        assert hist[0]["requests_2xx"] == 1
        assert hist[1]["hour"] == "2026-04-10T15:00:00Z"
        assert hist[1]["requests_2xx"] == 1


# ── rolling prune window ─────────────────────────────────────────────


class TestRollingPrune:
    def test_buckets_older_than_96h_pruned_on_new_hour(self, tmp_path):
        clock = Clock(datetime(2026, 4, 6, 0, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        # Internal: bucket exists for 00:00:00Z on 2026-04-06
        assert "2026-04-06T00:00:00Z" in s._hourly  # type: ignore[attr-defined]
        # Advance 97 hours → should trigger prune on new-hour creation.
        clock.advance(97 * 3600)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        assert "2026-04-06T00:00:00Z" not in s._hourly  # type: ignore[attr-defined]
        assert "2026-04-10T01:00:00Z" in s._hourly  # type: ignore[attr-defined]

    def test_buckets_exactly_96h_retained(self, tmp_path):
        clock = Clock(datetime(2026, 4, 6, 0, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        clock.advance(95 * 3600)  # 95 hours later → still in window
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        assert "2026-04-06T00:00:00Z" in s._hourly  # type: ignore[attr-defined]


# ── explicit zero-fill ───────────────────────────────────────────────


class TestZeroFill:
    def test_fresh_collector_returns_hours_rows_all_zero(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        hist = s.get_hourly_history(hours=96)
        assert len(hist) == 96
        assert all(r["requests_2xx"] == 0 for r in hist)
        assert all(r["requests_4xx"] == 0 for r in hist)
        assert all(r["requests_5xx"] == 0 for r in hist)
        assert all(r["cached"] == 0 for r in hist)
        assert all(r["avg_latency_ms"] is None for r in hist)

    def test_history_ordered_ascending_by_hour(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        hist = s.get_hourly_history(hours=5)
        assert len(hist) == 5
        hours = [r["hour"] for r in hist]
        assert hours == sorted(hours)
        assert hours[-1] == "2026-04-10T14:00:00Z"  # current hour is last

    def test_sparse_buckets_gap_filled(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 10, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        clock.advance(3 * 3600)  # 13:30
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        hist = s.get_hourly_history(hours=4)
        assert [r["hour"] for r in hist] == [
            "2026-04-10T10:00:00Z",
            "2026-04-10T11:00:00Z",
            "2026-04-10T12:00:00Z",
            "2026-04-10T13:00:00Z",
        ]
        assert hist[0]["requests_2xx"] == 1
        assert hist[1]["requests_2xx"] == 0
        assert hist[2]["requests_2xx"] == 0
        assert hist[3]["requests_2xx"] == 1


# ── persistence ──────────────────────────────────────────────────────


class TestPersistence:
    def test_new_hour_boundary_forces_save(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        # Advance past the hour boundary to trigger new-hour save (throttle
        # would normally block since it's <30s since init).
        clock.advance(3601)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        assert _pg_bucket("2026-04-10T14:00:00Z") is not None
        assert _pg_bucket("2026-04-10T15:00:00Z") is not None

    def test_throttle_blocks_rapid_writes(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        assert _pg_bucket("2026-04-10T14:00:00Z")["status_2xx"] == 1
        # Additional records within throttle window → no new write.
        clock.advance(5)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        clock.advance(5)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        # Still same persisted row.
        assert _pg_bucket("2026-04-10T14:00:00Z")["status_2xx"] == 1
        # After 30s another save should go through.
        clock.advance(25)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        assert _pg_bucket("2026-04-10T14:00:00Z")["status_2xx"] == 4

    def test_persist_load_round_trip(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        s.record("stock/AAPL/volatility", status=200, latency_ms=200.0)
        s.record("stock/AAPL/volatility", status=404, latency_ms=50.0)
        s.record("stock/AAPL/volatility", status=500, latency_ms=80.0)
        s.record("stock/AAPL/volatility", status=200, cached=True)
        s.flush_history()

        clock2 = Clock(datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc))
        s2 = _make_stats(tmp_path, clock2)
        row = s2.get_hourly_history(hours=1)[0]
        assert row["requests_2xx"] == 2
        assert row["requests_4xx"] == 1
        assert row["requests_5xx"] == 1
        assert row["cached"] == 1
        assert row["avg_latency_ms"] == pytest.approx(150.0)

    def test_flush_history_always_writes(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        # Force throttle window open, but flush_history should bypass it.
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        clock.advance(1)
        s.flush_history()
        assert _pg_bucket("2026-04-10T14:00:00Z")["status_2xx"] == 1

    def test_load_missing_file_clean_start(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        hist = s.get_hourly_history(hours=96)
        assert all(r["requests_2xx"] == 0 for r in hist)

    def test_load_corrupt_json_no_crash(self, tmp_path):
        history_path = tmp_path / "uw_api_stats_history.json"
        history_path.write_text("{not-json]]")
        clock = Clock(datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        # Should NOT raise.
        s = UWApiStats(now_fn=clock.now)
        hist = s.get_hourly_history(hours=1)
        assert hist[0]["requests_2xx"] == 0

    def test_load_forward_compat_unknown_keys(self, tmp_path):
        history_path = tmp_path / "uw_api_stats_history.json"
        payload = {
            "updated_at": "2026-04-10T14:00:00Z",
            "schema_version": 1,
            "buckets": {
                "2026-04-10T14:00:00Z": {
                    "requests_2xx": 5,
                    "requests_4xx": 1,
                    "requests_5xx": 0,
                    "cached": 2,
                    "sum_latency_ms": 500.0,
                    "latency_count": 5,
                    "future_field": "ignored",  # unknown key, forward-compat
                }
            },
            "extra_top_level": "also ignored",
        }
        history_path.write_text(json.dumps(payload))
        clock = Clock(datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc))
        s = UWApiStats(now_fn=clock.now)
        row = s.get_hourly_history(hours=1)[0]
        assert row["requests_2xx"] == 5
        assert row["requests_4xx"] == 1
        assert row["cached"] == 2
        assert row["avg_latency_ms"] == pytest.approx(100.0)

    def test_atomic_write_no_tempfile_left_on_success(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        s.flush_history()
        leftover = list(tmp_path.glob(".uw_api_stats_history_*"))
        assert leftover == []


# ── session counters rehydrated from persisted history ─────────────
# Regression: after FastAPI restart, the sidebar was showing zeros
# for requests/cache-hit/2xx-4xx-5xx even though the history file was
# loaded fine — because get_stats() only reflected in-memory session
# counters and _load_history() never seeded them.


class TestRehydrateSessionCountersOnLoad:
    def test_get_stats_reflects_loaded_history_after_restart(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        # 5 requests covering every class + a cache hit.
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        s.record("stock/AAPL/volatility", status=200, latency_ms=200.0)
        s.record("stock/AAPL/volatility", status=404, latency_ms=50.0)
        s.record("stock/AAPL/volatility", status=500, latency_ms=80.0)
        s.record("stock/AAPL/volatility", status=200, cached=True)
        s.flush_history()

        # Simulate process restart: new instance pointing at same file.
        clock2 = Clock(datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc))
        s2 = _make_stats(tmp_path, clock2)
        stats = s2.get_stats()

        # 5 real records: 2x 200, 1x 404, 1x 500, 1x cached-200.
        assert stats["totals"]["requests"] == 5
        assert stats["totals"]["success"] == 2
        assert stats["totals"]["cached"] == 1
        assert stats["totals"]["failures"] == 2
        # by_status is aggregated at class granularity — individual
        # codes (404/500) can't be recovered from hourly buckets.
        assert stats["by_status"].get(200) == 2
        assert stats["by_status"].get(400) == 1
        assert stats["by_status"].get(500) == 1
        # p95 latency is not rebuildable from sum/count — stays absent
        # until the first new live sample.
        assert "p95" not in stats["latency_ms"]

    def test_rehydrated_counters_merge_cleanly_with_live_records(self, tmp_path):
        """After rehydration, new record() calls increment the seeded
        counters rather than starting fresh — no drift, no double-count."""
        clock = Clock(datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        s.record("stock/AAPL/volatility", status=200, cached=True)
        s.flush_history()

        clock2 = Clock(datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc))
        s2 = _make_stats(tmp_path, clock2)
        # One fresh live request on top of the 2 historic ones.
        s2.record("stock/AAPL/volatility", status=200, latency_ms=123.0)
        stats = s2.get_stats()
        assert stats["totals"]["requests"] == 3
        assert stats["totals"]["success"] == 2  # 1 historic + 1 live
        assert stats["totals"]["cached"] == 1
        assert stats["by_status"][200] == 2  # historic 1 + live 1
        # p95 now has exactly one live sample.
        assert stats["latency_ms"]["p95"] == pytest.approx(123.0)

    def test_rehydrate_noop_when_no_history_file(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        stats = s.get_stats()
        assert stats["totals"]["requests"] == 0
        assert stats["by_status"] == {}


# ── reset vs clear_history ───────────────────────────────────────────


class TestResetPreservesHistory:
    def test_reset_keeps_hourly_buckets(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        s.reset()
        stats = s.get_stats()
        assert stats["totals"]["requests"] == 0
        hist = s.get_hourly_history(hours=1)
        assert hist[0]["requests_2xx"] == 1  # still there

    def test_clear_history_wipes_buckets(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        s.clear_history()
        hist = s.get_hourly_history(hours=1)
        assert hist[0]["requests_2xx"] == 0


# ── thread safety ─────────────────────────────────────────────────────


class TestThreadSafety:
    def test_concurrent_records_total_to_expected(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        errors: list[BaseException] = []

        def worker(tid: int) -> None:
            try:
                for _ in range(200):
                    s.record(f"stock/T{tid}/volatility", status=200, latency_ms=100.0)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        hist = s.get_hourly_history(hours=1)
        assert hist[0]["requests_2xx"] == 8 * 200


# ── get_stats backward compat ────────────────────────────────────────


class TestGetStatsShapeUnchanged:
    """Sidebar hook depends on the current get_stats() output shape —
    hourly buckets must not leak into or remove fields from it."""

    def test_get_stats_has_unchanged_top_level_keys(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        stats = s.get_stats()
        for key in (
            "session_started_at",
            "uptime_seconds",
            "totals",
            "latency_ms",
            "by_status",
            "by_ticker",
            "by_endpoint_type",
        ):
            assert key in stats, f"missing key: {key}"


# ── daily stats (8PM ET boundary) ───────────────────────────────────


class TestDailyStats:
    """get_daily_stats() sums hourly buckets since the most recent 8PM ET.

    8PM ET = midnight UTC during EDT (summer), 01:00 UTC during EST (winter).
    The method uses ZoneInfo("America/New_York") so DST is handled correctly.
    """

    def test_sums_buckets_since_8pm_et(self, tmp_path):
        """Records at 21:00 ET and 10:00 ET next day both count toward
        the same daily window (reset at 20:00 ET)."""
        # 2026-04-10 is EDT (UTC-4). 21:00 ET = 01:00 UTC Apr 11.
        clock = Clock(datetime(2026, 4, 11, 1, 30, tzinfo=timezone.utc))  # 21:30 ET Apr 10
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        s.record("stock/AAPL/volatility", status=404, latency_ms=50.0)

        # Advance to next day 14:00 ET = 18:00 UTC Apr 11 (still same daily window)
        clock.advance(16.5 * 3600)  # → 18:00 UTC Apr 11
        s.record("stock/AAPL/volatility", status=200, latency_ms=200.0)

        daily = s.get_daily_stats()
        assert daily["requests"] == 3  # 2xx + 4xx (non-cached)
        assert daily["requests_2xx"] == 2
        assert daily["requests_4xx"] == 1
        assert daily["requests_5xx"] == 0

    def test_excludes_buckets_before_8pm_et_boundary(self, tmp_path):
        """A record at 19:00 ET should belong to the previous daily window
        when viewed from 21:00 ET the same day."""
        # 19:00 ET Apr 10 = 23:00 UTC Apr 10 (EDT)
        clock = Clock(datetime(2026, 4, 10, 23, 30, tzinfo=timezone.utc))  # 19:30 ET
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)

        # Advance to 21:00 ET = 01:00 UTC Apr 11 (new daily window)
        clock.advance(2 * 3600)  # → 01:30 UTC Apr 11
        s.record("stock/AAPL/volatility", status=200, latency_ms=200.0)

        daily = s.get_daily_stats()
        # Only the 21:00 ET record counts — the 19:00 ET one is before the boundary.
        assert daily["requests"] == 1
        assert daily["requests_2xx"] == 1

    def test_returns_zeros_when_no_history(self, tmp_path):
        clock = Clock(datetime(2026, 4, 10, 14, 0, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        daily = s.get_daily_stats()
        assert daily["requests"] == 0
        assert daily["requests_2xx"] == 0
        assert daily["requests_4xx"] == 0
        assert daily["requests_5xx"] == 0
        assert daily["cached"] == 0
        assert daily["avg_latency_ms"] is None
        assert "reset_at" in daily

    def test_excludes_cached_hits_from_request_count(self, tmp_path):
        """Cached hits are local — they don't consume UW's 20k/day quota."""
        clock = Clock(datetime(2026, 4, 11, 1, 30, tzinfo=timezone.utc))  # 21:30 ET
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        s.record("stock/AAPL/volatility", status=200, cached=True)
        s.record("stock/AAPL/volatility", status=200, cached=True)

        daily = s.get_daily_stats()
        assert daily["requests"] == 1  # only the non-cached one
        assert daily["cached"] == 2
        assert daily["requests_2xx"] == 1

    def test_dst_transition_edt_to_est(self, tmp_path):
        """Fall back: Nov 1 2026, clocks fall back 2AM ET.
        After fall-back, 8PM Nov 1 is EST (UTC-5) = 01:00 UTC Nov 2.
        Compare with pre-fallback: 8PM Oct 31 EDT (UTC-4) = 00:00 UTC Nov 1."""
        # 01:30 UTC Nov 2 = 8:30 PM EST Nov 1 (post-fallback)
        clock = Clock(datetime(2026, 11, 2, 1, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)

        daily = s.get_daily_stats()
        assert daily["requests"] == 1
        # 8PM EST Nov 1 = 01:00 UTC Nov 2
        assert daily["reset_at"] == "2026-11-02T01:00:00Z"

    def test_dst_pre_fallback_uses_edt(self, tmp_path):
        """Before fall-back (Oct 31 EDT), 8PM ET = 00:00 UTC Nov 1."""
        # 00:30 UTC Nov 1 = 8:30 PM EDT Oct 31
        clock = Clock(datetime(2026, 11, 1, 0, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)

        daily = s.get_daily_stats()
        assert daily["requests"] == 1
        # 8PM EDT Oct 31 = 00:00 UTC Nov 1
        assert daily["reset_at"] == "2026-11-01T00:00:00Z"

    def test_dst_transition_est_to_edt(self, tmp_path):
        """Spring forward: Mar 8 2026, clocks spring forward 2AM ET.
        After spring-forward, 8PM Mar 8 is EDT (UTC-4) = 00:00 UTC Mar 9."""
        # Mar 8 2026 21:00 EDT = 01:00 UTC Mar 9
        clock = Clock(datetime(2026, 3, 9, 1, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)

        daily = s.get_daily_stats()
        assert daily["requests"] == 1
        # After spring-forward, 8PM EDT = 00:00 UTC
        assert daily["reset_at"] == "2026-03-09T00:00:00Z"

    def test_cache_hit_pct_in_daily(self, tmp_path):
        clock = Clock(datetime(2026, 4, 11, 1, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        s.record("stock/AAPL/volatility", status=200, cached=True)

        daily = s.get_daily_stats()
        # 1 real + 1 cached = 2 total API calls, 50% cache hit
        assert daily["cache_hit_pct"] == 50

    def test_avg_latency_in_daily(self, tmp_path):
        clock = Clock(datetime(2026, 4, 11, 1, 30, tzinfo=timezone.utc))
        s = _make_stats(tmp_path, clock)
        s.record("stock/AAPL/volatility", status=200, latency_ms=100.0)
        s.record("stock/AAPL/volatility", status=200, latency_ms=300.0)

        daily = s.get_daily_stats()
        assert daily["avg_latency_ms"] == pytest.approx(200.0)
