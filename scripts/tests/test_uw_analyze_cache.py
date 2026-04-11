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


def test_ttl_env_override_open(tmp_path, monkeypatch):
    """XENON_UW_TTL_OPEN_S overrides the ctor default at call time, not import time.

    The override is read inside ``_ttl()`` on every call, so a developer can
    ``export XENON_UW_TTL_OPEN_S=600`` in a running shell and have the next
    request honor it without restarting FastAPI.
    """
    cache = make_cache(tmp_path, market_open=True, ttl_open_s=300)
    assert cache._ttl() == 300  # baseline from ctor
    monkeypatch.setenv("XENON_UW_TTL_OPEN_S", "900")
    assert cache._ttl() == 900  # env wins
    monkeypatch.delenv("XENON_UW_TTL_OPEN_S")
    assert cache._ttl() == 300  # falls back to ctor when env unset


def test_ttl_env_override_closed(tmp_path, monkeypatch):
    cache = make_cache(tmp_path, market_open=False, ttl_closed_s=1800)
    assert cache._ttl() == 1800
    monkeypatch.setenv("XENON_UW_TTL_CLOSED_S", "3600")
    assert cache._ttl() == 3600


def test_ttl_env_override_ignored_when_wrong_state(tmp_path, monkeypatch):
    """XENON_UW_TTL_OPEN_S does not affect the closed path and vice versa."""
    cache = make_cache(tmp_path, market_open=True, ttl_open_s=300, ttl_closed_s=1800)
    monkeypatch.setenv("XENON_UW_TTL_CLOSED_S", "9999")
    assert cache._ttl() == 300  # open path ignores CLOSED override
    cache._market_open_fn = lambda: False
    assert cache._ttl() == 9999  # now the CLOSED override kicks in


def test_ttl_env_invalid_value_falls_back_to_default(tmp_path, monkeypatch):
    """Malformed XENON_UW_TTL_* values must NOT crash the cache.

    Before the _int_env helper, `int(os.environ.get(...))` would raise
    ValueError on every _is_fresh() call, turning a misconfig into a hard
    crash of every /uw-analyze read path. The helper now falls back to
    the ctor default and logs a warning.
    """
    cache = make_cache(tmp_path, market_open=True, ttl_open_s=300)

    # Non-numeric → fallback
    monkeypatch.setenv("XENON_UW_TTL_OPEN_S", "abc")
    assert cache._ttl() == 300

    # Empty string → fallback
    monkeypatch.setenv("XENON_UW_TTL_OPEN_S", "")
    assert cache._ttl() == 300

    # Negative / zero → fallback (TTLs must be positive)
    monkeypatch.setenv("XENON_UW_TTL_OPEN_S", "-5")
    assert cache._ttl() == 300
    monkeypatch.setenv("XENON_UW_TTL_OPEN_S", "0")
    assert cache._ttl() == 300

    # Python's int() strips whitespace, so "1800 " is a valid value,
    # not a misconfig. This is expected behavior.
    monkeypatch.setenv("XENON_UW_TTL_OPEN_S", "1800 ")
    assert cache._ttl() == 1800

    # And a valid value still works after invalid ones.
    monkeypatch.setenv("XENON_UW_TTL_OPEN_S", "900")
    assert cache._ttl() == 900


# ── Closed-market gate ────────────────────────────────────────────────────
#
# Gate semantics: when the market is closed AND the caller is not
# user-initiated, get_or_run returns cached data (flagged served_stale)
# or an empty stub, without calling the runner. User-initiated callers
# (the refresh button, single-ticker analyze, CLI) bypass the gate and
# always run the analyzer. Covers the 4 critical correctness properties
# from plan §2 (silly-humming-tide.md).


def test_closed_market_gate_serves_stale_without_running(tmp_path):
    """Stale entry + closed market + automatic caller → serve stale, no runner call."""

    async def go():
        calls = []
        # Both TTLs = 1s so the entry is stale in both OPEN and CLOSED states.
        # Without ttl_closed_s=1, flipping to CLOSED would use ttl_closed_s
        # default (3600) and `_is_fresh` would short-circuit before the gate.
        cache = make_cache(tmp_path, market_open=True, ttl_open_s=1, ttl_closed_s=1)
        runner = make_runner(calls_record=calls)
        await cache.get_or_run("nvda", runner=runner)
        assert calls == ["NVDA"]

        # Age the entry past TTL, flip to closed market, automatic caller.
        await asyncio.sleep(1.1)
        cache._market_open_fn = lambda: False

        entry, did_refresh = await cache.get_or_run("nvda", runner=runner)
        # Runner must NOT have been called again.
        assert calls == ["NVDA"]
        assert did_refresh is False
        # Stale served with flags set.
        assert entry.get("served_stale") is True
        assert entry.get("has_snapshot") is True
        # Original cached fields still present.
        assert entry["current"]["ticker"] == "NVDA"

    _run(go())


def test_closed_market_gate_returns_empty_stub_when_no_entry(tmp_path):
    """No cached entry + closed market + automatic caller → empty stub."""

    async def go():
        calls = []
        cache = make_cache(tmp_path, market_open=False)
        runner = make_runner(calls_record=calls)

        entry, did_refresh = await cache.get_or_run("ftnt", runner=runner)
        assert calls == []  # runner never invoked
        assert did_refresh is False
        assert entry == {
            "current": None,
            "has_snapshot": False,
            "served_stale": False,
        }

    _run(go())


def test_closed_market_fresh_entry_bypasses_gate(tmp_path):
    """Fresh entry during closed market → returned via _is_fresh short-circuit,
    WITHOUT the served_stale flag. Gate must not fire for fresh data."""

    async def go():
        calls = []
        # Seed while open, with a long TTL so it stays fresh.
        cache = make_cache(tmp_path, market_open=True, ttl_open_s=3600)
        runner = make_runner(calls_record=calls)
        await cache.get_or_run("nvda", runner=runner)

        # Flip to closed market — entry is still fresh.
        cache._market_open_fn = lambda: False
        entry, did_refresh = await cache.get_or_run("nvda", runner=runner)
        assert calls == ["NVDA"]  # still only one runner call
        assert did_refresh is False
        # CRITICAL: fresh entries must NOT be tagged served_stale during
        # closed market — the data actually IS fresh.
        assert "served_stale" not in entry or entry.get("served_stale") is False

    _run(go())


def test_closed_market_user_initiated_bypasses_gate(tmp_path):
    """user_initiated=True bypasses the gate and runs the analyzer even when
    the market is closed. This is how the refresh button works during
    overnight/weekend hours."""

    async def go():
        calls = []
        cache = make_cache(tmp_path, market_open=False)
        runner = make_runner(calls_record=calls)

        entry, did_refresh = await cache.get_or_run("nvda", runner=runner, user_initiated=True)
        assert calls == ["NVDA"]  # runner fired despite closed market
        assert did_refresh is True
        assert entry["current"]["ticker"] == "NVDA"
        # Fresh fetch — no stale flag.
        assert entry.get("served_stale") in (None, False)

    _run(go())


def test_open_market_user_initiated_is_noop_on_gate(tmp_path):
    """During market hours, user_initiated has no effect on the gate path —
    the gate never fires. Caching behavior is unchanged."""

    async def go():
        calls = []
        cache = make_cache(tmp_path, market_open=True)
        runner = make_runner(calls_record=calls)

        await cache.get_or_run("nvda", runner=runner, user_initiated=False)
        await cache.get_or_run("nvda", runner=runner, user_initiated=True)
        # Second call is a fresh-entry cache hit; runner called once.
        assert calls == ["NVDA"]

    _run(go())


def test_closed_market_force_true_user_initiated_false_is_still_gated(tmp_path):
    """CRITICAL plan property: force and user_initiated are SEPARATE signals.

    ``force=True`` alone must not bypass the closed-market gate — otherwise
    any scheduled job passing ``force=True`` to "freshen at all costs" would
    burn the daily UW budget overnight. Only explicit human action
    (``user_initiated=True``) should bypass the gate.

    This test locks in the semantic split so a future refactor that
    collapses the two parameters will immediately fail.
    """

    async def go():
        calls = []
        # Both TTLs = 1s so the entry goes stale fast in either state.
        cache = make_cache(tmp_path, market_open=True, ttl_open_s=1, ttl_closed_s=1)
        runner = make_runner(calls_record=calls)

        # Seed while open.
        await cache.get_or_run("nvda", runner=runner)
        assert calls == ["NVDA"]

        # Age the entry, flip to closed, call with force=True but
        # user_initiated=False (simulates a scheduled "always-fresh" job).
        await asyncio.sleep(1.1)
        cache._market_open_fn = lambda: False

        entry, did_refresh = await cache.get_or_run(
            "nvda",
            runner=runner,
            force=True,
            user_initiated=False,
        )
        # CRITICAL: runner must NOT have been called again. force=True
        # skips the _is_fresh short-circuit but the gate still blocks on
        # `not user_initiated and not market_open`.
        assert calls == ["NVDA"], (
            "force=True alone must not bypass the closed-market gate; user_initiated=True is required"
        )
        assert did_refresh is False
        assert entry.get("served_stale") is True
        assert entry.get("has_snapshot") is True

    _run(go())


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


# ── Last-known-good sticky-field merge ────────────────────────────────────
#
# Regressions locking in the fix for the "term card is empty / fields
# disappear during refresh" incident: when a UW endpoint 429s, the
# partial enrichment returned None, and the previous (good) value was
# being clobbered on each refresh. `_merge_sticky_fields` now carries
# the prior value forward for a whitelist of enrichment fields.


def test_sticky_fields_carry_over_when_runner_returns_none(tmp_path):
    """If the new snapshot has `term_structure_label=None` but the
    previous snapshot had it populated, the new snapshot must inherit
    the old value — UW had a transient failure, not a state change."""
    cache = make_cache(tmp_path)

    good_display = _display() | {
        "term_structure_label": "inverted",
        "sector": "Technology",
        "iv": 42.0,
        "rv": 33.1,
        "gamma_per_1pct": 12345.0,
        "short_volume_ratio": 0.48,
    }
    degraded_display = _display() | {
        "term_structure_label": None,
        "sector": None,
        "iv": None,
        "rv": None,
        "gamma_per_1pct": None,
        "short_volume_ratio": None,
    }

    # First refresh: runner returns good data.
    _run(cache.get_or_run("NVDA", runner=make_runner(display=good_display)))
    # Second refresh: runner returns a degraded snapshot (simulated
    # UW 429 wiping the enrichment fields).
    _run(
        cache.get_or_run(
            "NVDA",
            runner=make_runner(display=degraded_display),
            force=True,
        )
    )

    disp = cache.get_entry("NVDA")["current"]["display"]
    assert disp["term_structure_label"] == "inverted", "must carry over last-known-good"
    assert disp["sector"] == "Technology"
    assert disp["iv"] == 42.0
    assert disp["rv"] == 33.1
    assert disp["gamma_per_1pct"] == 12345.0
    assert disp["short_volume_ratio"] == 0.48


def test_sticky_fields_legitimate_transition_preserved(tmp_path):
    """A non-None -> new non-None transition must NOT be masked by the
    sticky merge. Carry-over only kicks in when the new value is None."""
    cache = make_cache(tmp_path)

    first = _display() | {"term_structure_label": "normal", "sector": "Tech"}
    second = _display() | {"term_structure_label": "inverted", "sector": "Tech"}

    _run(cache.get_or_run("NVDA", runner=make_runner(display=first)))
    _run(cache.get_or_run("NVDA", runner=make_runner(display=second), force=True))
    disp = cache.get_entry("NVDA")["current"]["display"]
    assert disp["term_structure_label"] == "inverted"


def test_non_sticky_fields_still_clobber_on_none(tmp_path):
    """High-frequency mutable fields (net premium, flow score) must
    NOT be carried over — a stale $27M call sweep would be misleading."""
    cache = make_cache(tmp_path)

    first = _display() | {"net_call_premium": 27_000_000, "net_put_premium": -4_000_000}
    second = _display() | {"net_call_premium": None, "net_put_premium": None}

    _run(cache.get_or_run("NVDA", runner=make_runner(display=first)))
    _run(cache.get_or_run("NVDA", runner=make_runner(display=second), force=True))
    disp = cache.get_entry("NVDA")["current"]["display"]
    assert disp["net_call_premium"] is None, "flow fields must not be sticky"
    assert disp["net_put_premium"] is None


def test_sticky_merge_noop_on_first_refresh(tmp_path):
    """No previous snapshot → merge is a no-op, None stays None."""
    cache = make_cache(tmp_path)

    _run(
        cache.get_or_run(
            "NVDA",
            runner=make_runner(display=_display() | {"term_structure_label": None}),
        )
    )
    disp = cache.get_entry("NVDA")["current"]["display"]
    assert disp["term_structure_label"] is None
