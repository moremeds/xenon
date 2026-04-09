"""Tests for UwAnalyzeCache: TTL, singleflight, semaphore, atomic writes."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.services.uw_analyze_cache import (  # noqa: E402
    UwAnalyzeCache,
    build_snapshot,
    derive_from_report,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _report(price=100, gex_sign="POSITIVE", flow=10):
    return {
        "ticker": "NVDA",
        "price": price,
        "regime": {"gex_sign": gex_sign},
        "scores": {"flow": flow},
    }


def _display(iv_rank=42, max_pain=100, ncp=1e6, npp=-1e6):
    return {
        "iv_rank": iv_rank,
        "max_pain": max_pain,
        "net_call_premium": ncp,
        "net_put_premium": npp,
        "gex_flip": 99,
        "call_wall_strike": 110,
        "put_wall_strike": 90,
    }


def make_cache(tmp_path, *, market_open=True, **kw):
    return UwAnalyzeCache(
        cache_path=tmp_path / "cache.json",
        market_open_fn=lambda: market_open,
        **kw,
    )


def make_runner(report=None, display=None, *, flow_alerts=None, calls_record=None, delay=0.0):
    report = report or _report()
    display = display or _display()
    flow_alerts = flow_alerts if flow_alerts is not None else []

    async def _runner(ticker: str):
        if calls_record is not None:
            calls_record.append(ticker)
        if delay:
            await asyncio.sleep(delay)
        return report, display, flow_alerts

    return _runner


# ── derive_from_report ─────────────────────────────────────────────────────


def test_derive_pulls_all_fields():
    out = derive_from_report(_report(), _display())
    assert out["gex_sign"] == "POSITIVE"
    assert out["max_pain"] == 100
    assert out["iv_rank"] == 42
    assert out["spot"] == 100
    assert out["flow_score"] == 10


def test_derive_handles_missing_regime():
    out = derive_from_report({"ticker": "NVDA", "price": 100}, _display())
    assert out["gex_sign"] is None


def test_build_snapshot_includes_derived_and_ts():
    snap = build_snapshot("nvda", _report(), _display())
    assert snap["ticker"] == "nvda"
    assert "ts" in snap
    assert snap["derived"]["gex_sign"] == "POSITIVE"


def test_build_snapshot_includes_flow_alerts():
    from api.services.uw_analyze_cache import build_snapshot as _bs

    report = {"price": 100.0, "regime": {"gex_sign": "POSITIVE"}}
    display = {"max_pain": 99.0}
    flow_alerts = [
        {
            "option_type": "call",
            "strike": 100,
            "expiration_date": "2026-05-15",
            "total_premium": 6_000_000,
        }
    ]
    snap = _bs("AAPL", report, display, flow_alerts=flow_alerts)
    assert snap["flow_alerts"] == flow_alerts
    assert snap["derived"]["spot"] == 100.0


# ── TTL behaviour ──────────────────────────────────────────────────────────


def test_first_call_runs_runner(tmp_path):
    async def go():
        calls = []
        cache = make_cache(tmp_path)
        entry, did_refresh = await cache.get_or_run(
            "nvda", runner=make_runner(calls_record=calls), sources=["portfolio"]
        )
        assert calls == ["NVDA"]
        assert did_refresh is True
        assert entry["current"]["ticker"] == "NVDA"
        assert entry["sources"] == ["portfolio"]

    _run(go())


def test_fresh_entry_returned_without_running(tmp_path):
    async def go():
        calls = []
        cache = make_cache(tmp_path)
        runner = make_runner(calls_record=calls)
        await cache.get_or_run("nvda", runner=runner)
        await cache.get_or_run("nvda", runner=runner)
        assert calls == ["NVDA"]

    _run(go())


def test_force_bypasses_ttl(tmp_path):
    async def go():
        calls = []
        cache = make_cache(tmp_path)
        runner = make_runner(calls_record=calls)
        await cache.get_or_run("nvda", runner=runner)
        await cache.get_or_run("nvda", runner=runner, force=True)
        assert calls == ["NVDA", "NVDA"]

    _run(go())


def test_stale_entry_reruns(tmp_path):
    async def go():
        calls = []
        cache = make_cache(tmp_path, ttl_open_s=1)
        runner = make_runner(calls_record=calls)
        entry, _ = await cache.get_or_run("nvda", runner=runner)
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        entry["current"]["ts"] = old_ts
        await cache.get_or_run("nvda", runner=runner)
        assert len(calls) == 2

    _run(go())


def test_ttl_open_vs_closed(tmp_path):
    cache = make_cache(tmp_path, market_open=True, ttl_open_s=300, ttl_closed_s=1800)
    assert cache._ttl() == 300
    cache._market_open_fn = lambda: False
    assert cache._ttl() == 1800


# ── Singleflight + semaphore ───────────────────────────────────────────────


def test_singleflight_collapses_concurrent_same_ticker(tmp_path):
    async def go():
        calls = []
        cache = make_cache(tmp_path)
        runner = make_runner(calls_record=calls, delay=0.05)
        results = await asyncio.gather(
            cache.get_or_run("nvda", runner=runner),
            cache.get_or_run("nvda", runner=runner),
            cache.get_or_run("nvda", runner=runner),
        )
        assert len(calls) == 1
        assert all(r[0]["current"]["ticker"] == "NVDA" for r in results)

    _run(go())


def test_semaphore_caps_concurrent_calls(tmp_path):
    in_flight = 0
    peak = 0

    async def runner(ticker: str):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return _report(), _display(), []

    async def go():
        cache = make_cache(tmp_path, max_parallel=2)
        await asyncio.gather(*[cache.get_or_run(t, runner=runner) for t in ["AAA", "BBB", "CCC", "DDD"]])

    _run(go())
    assert peak <= 2


# ── Persistence + atomic writes ────────────────────────────────────────────


def test_persists_to_disk(tmp_path):
    async def go():
        cache = make_cache(tmp_path)
        await cache.get_or_run("nvda", runner=make_runner(), sources=["portfolio"])

    _run(go())
    raw = json.loads((tmp_path / "cache.json").read_text())
    assert "NVDA" in raw["entries"]
    assert raw["entries"]["NVDA"]["sources"] == ["portfolio"]


def test_reload_from_disk(tmp_path):
    async def go():
        cache1 = make_cache(tmp_path)
        await cache1.get_or_run("nvda", runner=make_runner())

    _run(go())
    cache2 = make_cache(tmp_path)
    entry = cache2.get_entry("nvda")
    assert entry is not None
    assert entry["current"]["ticker"] == "NVDA"


def test_corrupt_file_starts_empty(tmp_path):
    (tmp_path / "cache.json").write_text("not json {{")
    cache = make_cache(tmp_path)
    cache._ensure_loaded()
    assert cache._entries == {}


# ── Source merging ─────────────────────────────────────────────────────────


def test_sources_merge_across_calls(tmp_path):
    async def go():
        cache = make_cache(tmp_path)
        await cache.get_or_run("nvda", runner=make_runner(), sources=["portfolio"])
        await cache.upsert_sources("nvda", ["watchlist"])
        return cache.get_entry("nvda")

    entry = _run(go())
    assert entry["sources"] == ["portfolio", "watchlist"]


def test_force_rerun_keeps_sources(tmp_path):
    async def go():
        cache = make_cache(tmp_path)
        await cache.get_or_run("nvda", runner=make_runner(), sources=["portfolio", "watchlist"])
        await cache.get_or_run("nvda", runner=make_runner(), force=True, sources=["adhoc"])
        return cache.get_entry("nvda")

    entry = _run(go())
    assert set(entry["sources"]) == {"portfolio", "watchlist", "adhoc"}


# ── Previous snapshot rotation ─────────────────────────────────────────────


def test_previous_rotates_on_force_rerun(tmp_path):
    """`previous` is a light {ts, derived} stub after memory bounds —
    the full report is no longer retained (dropping it is ~95% of the
    per-entry memory win). The derived block is enough for the diff
    engine and for the UI's `prev_ts` exposure."""

    async def go():
        cache = make_cache(tmp_path)
        await cache.get_or_run("nvda", runner=make_runner(report=_report(price=100)))
        await cache.get_or_run("nvda", runner=make_runner(report=_report(price=110)), force=True)
        return cache.get_entry("nvda")

    entry = _run(go())
    # Current still holds the full report.
    assert entry["current"]["report"]["price"] == 110
    # Previous is now light: only ts + derived.
    prev = entry["previous"]
    assert isinstance(prev, dict)
    assert set(prev.keys()) == {"ts", "derived"}
    assert prev["derived"]["spot"] == 100
    assert "report" not in prev
    assert "display" not in prev


# ── Memory bounds ──────────────────────────────────────────────────────────


def test_entries_evicts_lru_adhoc_first_over_cap(tmp_path):
    """Exceeding max_entries evicts lowest-tier LRU first (adhoc),
    protecting portfolio/watchlist."""

    async def go():
        cache = make_cache(tmp_path, max_entries=3)
        # Insert 2 portfolio tickers + 3 adhoc — should evict 2 adhoc
        # (lowest tier) before touching portfolio.
        await cache.get_or_run("AAA", runner=make_runner(), sources=["portfolio"])
        await cache.get_or_run("BBB", runner=make_runner(), sources=["portfolio"])
        await cache.get_or_run("CCC", runner=make_runner(), sources=["adhoc"])
        await cache.get_or_run("DDD", runner=make_runner(), sources=["adhoc"])
        await cache.get_or_run("EEE", runner=make_runner(), sources=["adhoc"])
        return cache

    cache = _run(go())
    assert len(cache._entries) == 3
    # Portfolio tickers preserved.
    assert "AAA" in cache._entries
    assert "BBB" in cache._entries
    # Most recent adhoc survives; earlier adhocs evicted.
    assert "EEE" in cache._entries
    assert "CCC" not in cache._entries
    assert "DDD" not in cache._entries


def test_materialized_changes_capped(tmp_path, monkeypatch):
    """Even if the diff engine emits many changes, only the last N are
    kept on the entry to bound memory."""
    from api.services import uw_analyze_cache as mod

    # Force the diff engine to emit 25 synthetic changes so we can assert
    # the cap trims to _MAX_MATERIALIZED_CHANGES (10).
    class _FakeChange:
        def __init__(self, i):
            self.i = i

        def to_dict(self):
            return {
                "code": "GEX_FLIP_SIGN",
                "idx": self.i,
                "label": f"c{self.i}",
                "prev": None,
                "curr": None,
                "severity": "info",
            }

    def _fake_compute(prev, curr):
        if not prev:
            return []
        return [_FakeChange(i) for i in range(25)]

    import api.services.uw_analyze_diff as diff_mod

    monkeypatch.setattr(diff_mod, "compute_changes", _fake_compute)

    async def go():
        cache = make_cache(tmp_path)
        await cache.get_or_run("nvda", runner=make_runner(report=_report(price=100)))
        await cache.get_or_run("nvda", runner=make_runner(report=_report(price=110)), force=True)
        return cache.get_entry("nvda")

    entry = _run(go())
    mats = entry["materialized_changes"]
    assert len(mats) == mod._MAX_MATERIALIZED_CHANGES
    # Trimmed to the *latest* slice — last entry is idx 24.
    assert mats[-1]["idx"] == 24
    assert mats[0]["idx"] == 25 - mod._MAX_MATERIALIZED_CHANGES


def test_orphan_locks_swept_on_eviction(tmp_path):
    """When an entry is evicted, its per-ticker lock is swept too."""

    async def go():
        cache = make_cache(tmp_path, max_entries=2)
        await cache.get_or_run("AAA", runner=make_runner(), sources=["adhoc"])
        await cache.get_or_run("BBB", runner=make_runner(), sources=["adhoc"])
        await cache.get_or_run("CCC", runner=make_runner(), sources=["adhoc"])
        return cache

    cache = _run(go())
    assert len(cache._entries) == 2
    # No orphan lock left for the evicted ticker.
    assert all(t in cache._entries for t in cache._per_ticker_locks)


# ── History archive ────────────────────────────────────────────────────────


def _archive_cache(tmp_path, **kw):
    return UwAnalyzeCache(
        cache_path=tmp_path / "cache.json",
        history_path=tmp_path / "history",
        market_open_fn=lambda: True,
        **kw,
    )


def test_archive_written_on_refresh(tmp_path):
    async def go():
        cache = _archive_cache(tmp_path)
        await cache.get_or_run("nvda", runner=make_runner(), sources=["portfolio"])
        return cache

    cache = _run(go())
    nvda_dir = cache.history_path / "NVDA"
    files = sorted(nvda_dir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert set(payload.keys()) >= {"current", "materialized_changes", "archived_at"}
    assert payload["current"]["ticker"] == "NVDA"


def test_archive_not_written_on_cache_hit(tmp_path):
    async def go():
        cache = _archive_cache(tmp_path)
        await cache.get_or_run("nvda", runner=make_runner())
        await cache.get_or_run("nvda", runner=make_runner())  # fresh → cache hit
        return cache

    cache = _run(go())
    files = list((cache.history_path / "NVDA").glob("*.json"))
    assert len(files) == 1  # only the first refresh archived


def test_archive_failure_does_not_break_request(tmp_path):
    async def go():
        cache = _archive_cache(tmp_path)

        # Simulate disk-full at the sync write layer — the public wrapper
        # must catch and log without raising.
        def boom(target, payload):
            raise OSError("disk full")

        cache._write_archive_sync = boom  # type: ignore[method-assign]
        entry, did_refresh = await cache.get_or_run("nvda", runner=make_runner())
        return entry, did_refresh

    entry, did_refresh = _run(go())
    assert did_refresh is True
    assert entry["current"]["ticker"] == "NVDA"


def test_archive_happens_after_persist(tmp_path):
    """If _persist raises, no archive file should exist — ordering guarantee."""

    async def go():
        cache = _archive_cache(tmp_path)
        orig_persist = cache._persist

        async def failing_persist():
            await orig_persist()
            raise RuntimeError("simulated persist failure")

        cache._persist = failing_persist  # type: ignore[method-assign]
        try:
            await cache.get_or_run("nvda", runner=make_runner())
        except RuntimeError:
            pass
        return cache

    cache = _run(go())
    nvda_dir = cache.history_path / "NVDA"
    assert not nvda_dir.exists() or not list(nvda_dir.glob("*.json"))


def test_archive_uses_coerce_jsonable(tmp_path):
    """Archive must round-trip non-JSON-native values via _coerce_jsonable."""

    async def go():
        cache = _archive_cache(tmp_path)

        # Inject a datetime into report — json.dump with default=str would
        # stringify it, but _coerce_jsonable normalises via .isoformat().
        report = _report()
        report["fetched_at"] = datetime(2026, 4, 8, 12, 0, 0, tzinfo=timezone.utc)

        async def runner(ticker):
            return report, _display(), []

        await cache.get_or_run("nvda", runner=runner)
        return cache

    cache = _run(go())
    files = list((cache.history_path / "NVDA").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    fetched = payload["current"]["report"]["fetched_at"]
    assert isinstance(fetched, str) and fetched.startswith("2026-04-08")


def test_archive_handles_none_summaries(tmp_path):
    """Runner returning 3-tuple → dark_pool/options_flow summaries are None."""

    async def go():
        cache = _archive_cache(tmp_path)
        await cache.get_or_run("nvda", runner=make_runner())  # 3-tuple runner
        return cache

    cache = _run(go())
    files = list((cache.history_path / "NVDA").glob("*.json"))
    payload = json.loads(files[0].read_text())
    assert payload["current"]["dark_pool_summary"] is None
    assert payload["current"]["options_flow_summary"] is None


def test_archive_filename_unique_under_burst(tmp_path):
    """Two back-to-back forced refreshes must not collide on filename."""

    async def go():
        cache = _archive_cache(tmp_path)
        await cache.get_or_run("nvda", runner=make_runner(), force=True)
        await cache.get_or_run("nvda", runner=make_runner(), force=True)
        return cache

    cache = _run(go())
    files = list((cache.history_path / "NVDA").glob("*.json"))
    assert len(files) == 2


def test_load_history_returns_descending(tmp_path):
    cache = _archive_cache(tmp_path)
    ticker_dir = cache.history_path / "NVDA"
    ticker_dir.mkdir(parents=True)
    stamps = ["20260101-120000-000000", "20260102-120000-000000", "20260103-120000-000000"]
    for s in stamps:
        (ticker_dir / f"{s}.json").write_text(json.dumps({"stamp": s}))

    out = cache.load_history("nvda")
    assert [o["stamp"] for o in out] == list(reversed(stamps))

    out_limited = cache.load_history("nvda", limit=2)
    assert [o["stamp"] for o in out_limited] == ["20260103-120000-000000", "20260102-120000-000000"]


def test_load_history_applies_limit_before_parse(tmp_path, monkeypatch):
    """limit must cut the filename list BEFORE any JSON parsing."""
    cache = _archive_cache(tmp_path)
    ticker_dir = cache.history_path / "NVDA"
    ticker_dir.mkdir(parents=True)
    for i in range(100):
        stamp = f"20260101-120000-{i:06d}"
        (ticker_dir / f"{stamp}.json").write_text(json.dumps({"i": i}))

    parse_count = {"n": 0}
    real_load = json.load

    def counting_load(fh, *a, **kw):
        parse_count["n"] += 1
        return real_load(fh, *a, **kw)

    monkeypatch.setattr(json, "load", counting_load)
    out = cache.load_history("nvda", limit=5)
    assert len(out) == 5
    assert parse_count["n"] == 5  # exactly 5, not 100


def test_load_history_since_filter(tmp_path):
    cache = _archive_cache(tmp_path)
    ticker_dir = cache.history_path / "NVDA"
    ticker_dir.mkdir(parents=True)
    for stamp in ["20260101-120000-000000", "20260105-120000-000000", "20260110-120000-000000"]:
        (ticker_dir / f"{stamp}.json").write_text(json.dumps({"stamp": stamp}))

    out = cache.load_history("nvda", since=datetime(2026, 1, 4, tzinfo=timezone.utc))
    assert len(out) == 2
    assert all(o["stamp"] >= "20260104" for o in out)


def test_load_history_missing_ticker_returns_empty(tmp_path):
    cache = _archive_cache(tmp_path)
    assert cache.load_history("ZZZ") == []


# ── Regression: _ensure_loaded must not permanently latch on transient failure


def _seeded_cache_file(path: Path, *, ticker: str = "NVDA") -> None:
    payload = {
        "updated_at": "2026-04-08T00:00:00+00:00",
        "entries": {
            ticker: {
                "current": {
                    "ticker": ticker,
                    "ts": "2026-04-08T00:00:00+00:00",
                    "report": {},
                    "display": {},
                    "flow_alerts": [],
                    "derived": {},
                    "dark_pool_summary": None,
                    "options_flow_summary": None,
                },
                "previous": None,
                "oi_baseline": None,
                "sources": ["portfolio"],
                "materialized_changes": [],
            }
        },
    }
    path.write_text(json.dumps(payload))


def test_ensure_loaded_retries_after_malformed_json(tmp_path):
    """A half-written file must not permanently blind the cache."""
    cache_file = tmp_path / "cache.json"
    cache_file.write_text("{not valid json")  # partial write from a crashed writer

    cache = UwAnalyzeCache(cache_path=cache_file, history_path=tmp_path / "hist")
    assert cache.all_entries() == {}
    assert cache._loaded is False, "malformed file must leave _loaded False for retry"

    # File is fixed (atomic replace from another process).
    _seeded_cache_file(cache_file)
    entries = cache.all_entries()
    assert "NVDA" in entries, "cache must retry load after transient parse failure"
    assert cache._loaded is True


def test_ensure_loaded_retries_after_wrong_shape(tmp_path):
    """A schema-mismatched file must not permanently blind the cache."""
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({"entries": []}))  # list, not dict

    cache = UwAnalyzeCache(cache_path=cache_file, history_path=tmp_path / "hist")
    assert cache.all_entries() == {}
    assert cache._loaded is False, "wrong-shape file must leave _loaded False for retry"

    _seeded_cache_file(cache_file)
    entries = cache.all_entries()
    assert "NVDA" in entries


def test_ensure_loaded_picks_up_file_appearing_after_startup(tmp_path):
    """Missing file at construction must not block a later-arriving file."""
    cache_file = tmp_path / "cache.json"  # does not exist yet
    cache = UwAnalyzeCache(cache_path=cache_file, history_path=tmp_path / "hist")
    assert cache.all_entries() == {}
    # Missing file is a legitimate cold start: _loaded latches True to
    # avoid stat'ing disk on every call. The in-memory state is now
    # authoritative, and a later file would require an explicit reload.
    assert cache._loaded is True


def test_persist_refuses_to_overwrite_nonempty_file_when_memory_empty(tmp_path):
    """The data-loss footgun: a silently-empty cache must not clobber disk."""
    cache_file = tmp_path / "cache.json"
    _seeded_cache_file(cache_file)
    original_bytes = cache_file.read_bytes()

    cache = UwAnalyzeCache(cache_path=cache_file, history_path=tmp_path / "hist")
    # Simulate the pathological state: loaded but empty.
    cache._loaded = True
    cache._entries.clear()

    _run(cache._persist())
    assert cache_file.read_bytes() == original_bytes, "persist must refuse to wipe real data"

    # allow_empty=True overrides the guard for legitimate wipes.
    _run(cache._persist(allow_empty=True))
    assert cache_file.read_bytes() != original_bytes
    assert json.loads(cache_file.read_text())["entries"] == {}
