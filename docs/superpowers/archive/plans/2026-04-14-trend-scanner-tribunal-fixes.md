# Trend Scanner Tribunal Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## Revision v2 — post Codex+Gemini+Claude tribunal

Changes from v1 (after tribunal found 12 critical/important issues):

- **Class correction:** `TrendScanner` does not exist. All `self._*` references retargeted to the real class `LiveTrendDataFetcher` (`scripts/trend_scan.py:189`) with `self.uw_client` (no leading underscore).
- **Module correction:** `score_volume_profile()` and `detect_breakout()` both live in `ta_prefilter.py` and are called from `compute_trend_score()` there, not from `trend_scan.py`. Tasks 5, 6, and 9 retargeted to `compute_trend_score`.
- **Stage A refactor (now explicit in Task 9):** `_stage_a()` hard-gates on `passes_bullish_gate()`. For bearish candidates to survive, Stage A must be split into neutral data fetch + directional gate evaluation.
- **Storage + web migration added to Task 8:** Removing `suggested_trade` breaks DuckDB schema in `scripts/trend_scan_lib/storage.py`, tests in `test_trend_storage.py`, TS types in `web/lib/types.ts`, and rendering in `web/components/WorkspaceSections.tsx`. New sub-tasks handle these.
- **Scoring component preservation (Task 9 Step 6):** `compute_structure_score()` rewrite now preserves the 4-component composition (`score_gamma_flip`, `score_net_gex`, `score_max_pain`, `score_oi_change`) weighted by `STRUCTURE_WEIGHTS`. Only the wall-support component branches on direction.
- **`scores=` restored:** Every `TrendCandidate(...)` constructor example now passes `scores=scores` (was silently dropped in v1).
- **Task 9 integration test rewritten:** Old test referenced nonexistent APIs (`ts.TAService`, `ts.build_confluence`, `run_scan_pipeline(top=...)`). New test uses real `run_scan_pipeline(cfg, *, data_fetcher, uw_client, ib_client, db_path)` signature and a fake `DataFetcher`.
- **Weight rebalancing specified exactly** (Task 10): explicit 5-key weight dict.
- **`earnings_days` sourced from `bc["vol_data"]`** (Task 10), not `ohlcv` — it comes from `fetch_volatility()`, not OHLCV.
- **UNIVERSE_CACHE consumed by scanner** (new Task 3b): cache is now read, not just written.
- **`--audit-only` stays offline** (Task 3): static universe only; triple-source build only in refresh mode.
- **`up_day_volume_ratio` sentinel softened** (Task 4): 1.0 when either side has <3 samples; no 2.0 spike.
- **Flow confirmation direction-awareness added** as a new Task 9b — originally hand-waved.
- **Catalyst serialization** (Task 10 + storage sub-task): list stored as JSON string in DuckDB.

Phase ordering unchanged (1 → 2 → 3). Task count grew from 10 to 12.

---

**Goal:** Address the nine tribunal findings against `feat/ta-integration` in three phases: defensive hardening (items 5–7), signal accuracy (items 3, 8, 9), and scope expansion (items 1, 2, 4).

**Architecture:** Scanner stays analysis-only. It emits ranked signal candidates with structure _hints_, not vetted trades; Four Gates (Convexity/Edge/Risk/No-Naked-Short) remain enforced at order-routing time, not here. Bullish + bearish pipelines run in parallel with mirrored scoring. TA freshness is unified behind `TAService._is_stale()` as the single source of truth. Pre-market prep warms the full triple-source scanner universe, not just the static slice. A new Stage C catalyst check fetches UW headlines when a UW client is available, degrading gracefully otherwise.

**Tech Stack:** Python 3.13, DuckDB, pandas, ib_insync, Unusual Whales client, pytest. No new runtime dependencies.

---

## Scoping Decision (Locked In Before Phase 3)

**Finding #2 — Analysis-only vs gate-enforced.** This plan commits to **analysis-only**. Scanner output:

- Keeps a `structure_hint` string (derived from IV rank / vol regime / OI structure) for informational use.
- **Adds** `flags: ["four_gates_not_applied"]` on every candidate so consumers cannot mistake output for a trade instruction.
- **Removes** the `suggested_trade` field from `TrendCandidate` — consumers must compose trades through the order-routing machinery where Gate 4 (no naked shorts) actually lives.

If this decision is reversed, Phase 3 Task 8 becomes instead "route every structure through gate machinery" — roughly 3× the code and requires integrating `scripts/api/services/gate_machinery.py` (if it exists) or building one. Flag this with the author before starting Task 8.

---

## File Structure

### Modified files

- `scripts/trend_scan.py` — SPY crash guard, dual-direction loop, strip `suggested_trade`, thread catalyst data.
- `scripts/trend_scan_lib/stages/ta_prefilter.py` — verify breakout, add `passes_bearish_gate`, expose `detect_breakdown`.
- `scripts/trend_scan_lib/stages/options_structure.py` — overhead-wall reject, mirrored bearish structure scoring.
- `scripts/trend_scan_lib/stages/volatility.py` — up-day volume isolation, remove `suggest_trade_type` call sites.
- `scripts/trend_scan_lib/stages/flow_confirmation.py` — Stage C catalyst fetch + scoring.
- `scripts/trend_scan_lib/universe.py` — export universe-building helper for prep reuse.
- `scripts/trend_scan_lib/models.py` — add `structure_hint`, `catalysts` fields; remove `suggested_trade`.
- `scripts/ta_lib/service.py` — extend snapshot with `high_20d`, `up_day_volume_ratio`.
- `scripts/ta_premarket_prep.py` — use full universe, delegate staleness to TAService, persist universe.
- `scripts/api/server.py` — no change expected unless catch-up call signature changes.

### New files

- `scripts/trend_scan_lib/stages/catalysts.py` — catalyst fetch + scoring (headlines + earnings/FDA/guidance flags).
- `scripts/tests/test_trend_scan_catalysts.py` — unit tests for catalyst stage.
- `scripts/tests/test_trend_scan_bearish.py` — bearish pipeline tests.

### Tests touched

- `scripts/tests/test_ta_lib/test_service.py` — add snapshot field assertions.
- `scripts/tests/test_ta_lib/test_snapshot_contract.py` — update contract.
- `scripts/tests/test_ta_lib/test_premarket_prep.py` — new universe + staleness behavior.
- `scripts/tests/test_trend_scan_e2e.py` — mock dict needs new fields, dual direction.
- `scripts/tests/test_trend_ranking.py` — mirrored direction ranking.

---

# Phase 1 — Defensive Hardening

Goal: remove crash risk and consistency bugs. No behavior change to signal logic. Ship as independent commits.

---

### Task 1: SPY pre-cache crash guard (Finding #7)

**Files:**

- Modify: `scripts/trend_scan.py:201-204`
- Test: `scripts/tests/test_trend_scan_runtime.py`

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_trend_scan_runtime.py`:

```python
def test_pre_cache_spy_swallows_failure():
    """If SPY indicators fetch raises, pre_cache_spy must not propagate —
    scan continues with rs_vs_spy=1.0 default via existing branch in fetch_ohlcv."""
    from unittest.mock import MagicMock
    from scripts.trend_scan import LiveTrendDataFetcher

    failing_svc = MagicMock()
    failing_svc.get_indicators.side_effect = RuntimeError("SPY cold")

    fetcher = LiveTrendDataFetcher(uw_client=MagicMock(), ta_service=failing_svc)

    # Must not raise
    fetcher.pre_cache_spy()

    # Must leave _spy_df as None so fetch_ohlcv falls back to rs_vs_spy=1.0
    assert fetcher._spy_df is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.13 -m pytest scripts/tests/test_trend_scan_runtime.py::test_pre_cache_spy_swallows_failure -xvs`
Expected: FAIL with `RuntimeError: SPY cold`

- [ ] **Step 3: Implement the guard**

Replace `scripts/trend_scan.py:201-204`:

```python
def pre_cache_spy(self) -> None:
    """Cache SPY indicator DataFrame for rs_vs_spy calculations.

    Failure is non-fatal — RS benchmark is a nice-to-have; scan proceeds
    with rs_vs_spy=1.0 fallback if SPY is cold and IB unavailable.
    """
    if self._ta_service is None:
        return
    try:
        self._spy_df = self._ta_service.get_indicators("SPY", allow_fetch=False)
    except Exception as exc:
        logger.warning("pre_cache_spy: SPY unavailable (%s) — falling back to rs_vs_spy=1.0", exc)
        self._spy_df = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3.13 -m pytest scripts/tests/test_trend_scan_runtime.py::test_pre_cache_spy_swallows_failure -xvs`
Expected: PASS

- [ ] **Step 5: Run the full runtime test module**

Run: `python3.13 -m pytest scripts/tests/test_trend_scan_runtime.py -xvs`
Expected: all tests PASS (no regressions).

- [ ] **Step 6: Commit**

```bash
git add scripts/trend_scan.py scripts/tests/test_trend_scan_runtime.py
git commit -m "fix(trend_scan): guard pre_cache_spy against cold SPY

RS benchmark is informational — scan must not abort when SPY is cold
and IB is unavailable. Logs warning and falls back to rs_vs_spy=1.0
via the existing ticker.upper() != 'SPY' and self._spy_df is not None
branch in fetch_ohlcv."
```

---

### Task 2: Unify staleness check in premarket prep (Finding #6)

**Files:**

- Modify: `scripts/ta_premarket_prep.py:47-66`
- Test: `scripts/tests/test_ta_lib/test_premarket_prep.py`

- [ ] **Step 1: Read the current test fixture setup**

Run: `python3.13 -m pytest scripts/tests/test_ta_lib/test_premarket_prep.py --collect-only`
Identify the fixture that seeds DuckDB with OHLC bars. We'll extend it to cover the "bars but no indicators" case.

- [ ] **Step 2: Write the failing test**

Append to `scripts/tests/test_ta_lib/test_premarket_prep.py`:

```python
def test_classify_tickers_treats_missing_indicators_as_stale(tmp_path):
    """A ticker with current OHLC bars but NO ta_indicators row must
    classify as stale, matching TAService._is_stale() semantics.

    Regression: prior classify_tickers() only checked get_latest_bar_date,
    so a partially-seeded cache looked 'current' to the audit but was
    still refetched by TAService on every scan request."""
    from scripts.ta_lib.store import get_connection, init_schema, write_bars
    from scripts.ta_premarket_prep import classify_tickers
    from datetime import date
    import pandas as pd

    db = tmp_path / "ta.duckdb"
    conn = get_connection(str(db))
    init_schema(conn)

    today = date.today()
    bars = pd.DataFrame({
        "date": [pd.Timestamp(today)],
        "open": [100.0], "high": [101.0], "low": [99.0],
        "close": [100.5], "volume": [1_000_000],
    })
    write_bars(conn, "AAPL", "1d", bars)
    # NOTE: no ta_indicators row written — simulates a partial cache

    result = classify_tickers(conn, ["AAPL"], today)

    assert "AAPL" in result["stale"], (
        f"AAPL should be stale (no indicators) but got {result}"
    )
    assert "AAPL" not in result["current"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3.13 -m pytest scripts/tests/test_ta_lib/test_premarket_prep.py::test_classify_tickers_treats_missing_indicators_as_stale -xvs`
Expected: FAIL — "AAPL should be stale (no indicators)" (classify_tickers currently returns it as current).

- [ ] **Step 4: Refactor classify_tickers to delegate to TAService**

Replace `scripts/ta_premarket_prep.py:47-66`:

```python
def classify_tickers(
    conn,
    tickers: list[str],
    ref_date: date,
) -> dict[str, list[str]]:
    """Classify tickers as current / stale / missing.

    Delegates the freshness decision to TAService._is_stale() so audit
    and scanner agree. A ticker is 'current' iff it has bars AND
    indicators AND the most recent bar is from ref_date or later.
    """
    from scripts.ta_lib.service import TAService  # lazy import to avoid IB dep at module load

    # Build a read-only TAService bound to this connection.
    svc = TAService.__new__(TAService)
    svc._conn = conn
    svc._ib_client = None

    current: list[str] = []
    stale: list[str] = []
    missing: list[str] = []

    for t in tickers:
        latest = get_latest_bar_date(conn, t, "1d")
        if latest is None:
            missing.append(t)
            continue
        # Use _is_stale() — this catches "bars present but no indicators"
        # and applies the same ET-aware logic scanners use.
        if svc._is_stale(t, "1d", cursor=conn):
            stale.append(t)
        else:
            current.append(t)

    return {"current": current, "stale": stale, "missing": missing}
```

- [ ] **Step 5: Run tests to verify pass + no regressions**

Run: `python3.13 -m pytest scripts/tests/test_ta_lib/test_premarket_prep.py -xvs`
Expected: all tests PASS (including the new test and all pre-existing).

- [ ] **Step 6: Verify TAService construction path is safe**

Run: `python3.13 -m pytest scripts/tests/test_ta_lib/ -xvs`
Expected: PASS — confirm the `TAService.__new__(...)` bypass does not break any test assumption about `__init__` side-effects.

- [ ] **Step 7: Commit**

```bash
git add scripts/ta_premarket_prep.py scripts/tests/test_ta_lib/test_premarket_prep.py
git commit -m "fix(ta_premarket_prep): delegate staleness to TAService._is_stale

classify_tickers() previously only checked get_latest_bar_date,
so tickers with OHLC bars but missing ta_indicators rows were
reported 'current' to the audit while TAService correctly treated
them as stale. Now audit and service share one truth.

Unified semantics also inherit intraday 2h staleness and ET-aware
session detection for free."
```

---

### Task 3: Premarket prep warms full scanner universe (Finding #5)

**Files:**

- Modify: `scripts/ta_premarket_prep.py:101-104` (universe building)
- Modify: `scripts/ta_premarket_prep.py:132-140` (refresh block)
- Modify: `scripts/api/server.py:164-188` (trend_scan loop reads prep-persisted universe)
- Test: `scripts/tests/test_ta_lib/test_premarket_prep.py`

**Scoping contract (post-tribunal):**

- `--audit-only` MUST remain offline. Static universe only.
- `--force` and the default refresh path use the full triple-source universe via `build_universe(TrendScanConfig(), uw_client=..., ib_client=...)`.
- Config values come from real `TrendScanConfig()` defaults — never from an ad-hoc `SimpleNamespace`. This ensures `uw_flow_lookback_days=5` (the scanner default), not a custom `=1`.
- The persisted `data/ta_premarket_universe.json` is consumed by the scanner (Task 3b below).

- [ ] **Step 1: Write the failing test**

Append to `scripts/tests/test_ta_lib/test_premarket_prep.py`:

```python
def test_refresh_path_uses_full_universe_when_clients_available(tmp_path, monkeypatch):
    """When clients are available and --audit-only is NOT set, prep must warm
    the same triple-source universe the scanner will use (static + UW flow
    + IB scanner), using real TrendScanConfig defaults."""
    from unittest.mock import MagicMock
    import scripts.ta_premarket_prep as prep

    fake_uw = MagicMock()
    fake_ib = MagicMock()
    monkeypatch.setattr(prep, "_connect_uw_client", lambda: fake_uw, raising=False)
    monkeypatch.setattr(prep, "_connect_ib_client", lambda: fake_ib, raising=False)

    captured = {}
    def fake_build_universe(cfg, *, uw_client, ib_client):
        captured["cfg"] = cfg
        captured["uw"] = uw_client
        captured["ib"] = ib_client
        return ["AAPL", "TSLA", "XYZFLOW"]
    monkeypatch.setattr(prep, "build_universe", fake_build_universe, raising=False)

    monkeypatch.setattr(prep, "get_connection", lambda p: MagicMock())
    monkeypatch.setattr(prep, "init_schema", lambda c: None)
    monkeypatch.setattr(prep, "classify_tickers",
                        lambda c, t, d: {"current": t, "stale": [], "missing": []})
    # Make TAService / bulk_refresh no-ops
    fake_svc = MagicMock()
    fake_svc.bulk_refresh.return_value = None
    monkeypatch.setattr(prep, "TAService", lambda **k: fake_svc, raising=False)

    db = tmp_path / "ta.duckdb"
    prep.main(["--db", str(db)])  # NO --audit-only

    assert captured["uw"] is fake_uw
    assert captured["ib"] is fake_ib
    # Verify real config defaults were used — NOT an ad-hoc SimpleNamespace
    from scripts.trend_scan_lib.config import TrendScanConfig
    assert captured["cfg"].uw_flow_lookback_days == TrendScanConfig().uw_flow_lookback_days == 5
    assert captured["cfg"].uw_flow_min_premium == TrendScanConfig().uw_flow_min_premium == 100_000


def test_audit_only_stays_offline(tmp_path, monkeypatch):
    """--audit-only MUST NOT connect to UW or IB, even if clients would
    be available. Static universe + SPY only."""
    from unittest.mock import MagicMock
    import scripts.ta_premarket_prep as prep

    uw_called = ib_called = False
    def explode_uw():
        nonlocal uw_called
        uw_called = True
        return MagicMock()
    def explode_ib():
        nonlocal ib_called
        ib_called = True
        return MagicMock()
    monkeypatch.setattr(prep, "_connect_uw_client", explode_uw, raising=False)
    monkeypatch.setattr(prep, "_connect_ib_client", explode_ib, raising=False)

    monkeypatch.setattr(prep, "get_connection", lambda p: MagicMock())
    monkeypatch.setattr(prep, "init_schema", lambda c: None)
    monkeypatch.setattr(prep, "classify_tickers",
                        lambda c, t, d: {"current": t, "stale": [], "missing": []})

    db = tmp_path / "ta.duckdb"
    prep.main(["--audit-only", "--db", str(db)])

    assert not uw_called, "--audit-only must not connect to UW"
    assert not ib_called, "--audit-only must not connect to IB"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.13 -m pytest scripts/tests/test_ta_lib/test_premarket_prep.py::test_main_uses_full_universe_when_clients_available -xvs`
Expected: FAIL — `build_universe` is not currently called in prep; only `build_static_universe`.

- [ ] **Step 3: Extend `scripts/trend_scan_lib/universe.py` with a prep-friendly entry point**

No structural change needed — `build_universe` already exists at line 78. We just need prep to call it with optional clients.

- [ ] **Step 4: Rewrite `scripts/ta_premarket_prep.py` main block**

Replace lines 27-34 and 87-139 with:

```python
from scripts.ta_lib.store import get_connection, get_latest_bar_date, init_schema
from scripts.trend_scan_lib.config import TrendScanConfig
from scripts.trend_scan_lib.universe import build_universe, build_static_universe
from scripts.utils.market_calendar import get_last_n_trading_days

logger = logging.getLogger(__name__)

DEFAULT_DB = "data/ta.duckdb"
DEFAULT_SP500 = "data/universe/sp500.json"
DEFAULT_NASDAQ100 = "data/universe/nasdaq100.json"
UNIVERSE_CACHE = Path("data/ta_premarket_universe.json")


def _connect_uw_client():
    """Return a UW client or None if unavailable — non-fatal."""
    try:
        from scripts.clients.uw_client import UWClient
        return UWClient()
    except Exception as exc:
        logger.warning("UW client unavailable: %s — prep will omit UW flow tickers", exc)
        return None


def _connect_ib_client():
    """Return an IB client or None if unavailable — non-fatal."""
    try:
        from scripts.clients.ib_client import IBClient
        ib = IBClient()
        ib.connect(client_id="auto")
        return ib
    except Exception as exc:
        logger.warning("IB client unavailable: %s — prep will skip IB scanner universe", exc)
        return None


def _build_triple_source_universe(cfg: TrendScanConfig) -> tuple[list[str], object | None, object | None]:
    """Build the full scanner universe (refresh mode only).

    Never called from --audit-only. Uses real TrendScanConfig defaults so
    prep sees the same UW lookback / premium threshold the scanner does."""
    uw = _connect_uw_client()
    ib = _connect_ib_client()
    try:
        tickers = build_universe(cfg, uw_client=uw, ib_client=ib)
    except Exception as exc:
        logger.warning("build_universe failed (%s) — falling back to static-only", exc)
        tickers = build_static_universe(
            sp500_path=cfg.sp500_path,
            nasdaq100_path=cfg.nasdaq100_path,
        )
    if "SPY" not in tickers:
        tickers.append("SPY")
    return tickers, uw, ib


def _build_static_only_universe(args) -> list[str]:
    """Build static-only universe for --audit-only. Never touches network."""
    tickers = build_static_universe(sp500_path=args.sp500, nasdaq100_path=args.nasdaq100)
    if "SPY" not in tickers:
        tickers.append("SPY")
    return tickers
```

And update the IB connect block at lines 120-132 of `main()` to reuse `ib_client_preopened` when present:

Rewrite the `main()` function body to split the two paths:

```python
def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Pre-market TA data prep")
    parser.add_argument("--audit-only", action="store_true", help="Audit only, no IB refresh. Offline.")
    parser.add_argument("--force", action="store_true", help="Refresh all tickers, not just stale/missing")
    parser.add_argument("--db", default=DEFAULT_DB, help="DuckDB path")
    parser.add_argument("--sp500", default=DEFAULT_SP500, help="SP500 universe JSON")
    parser.add_argument("--nasdaq100", default=DEFAULT_NASDAQ100, help="NASDAQ100 universe JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # Audit phase uses a static universe — no network.
    conn = get_connection(args.db)
    init_schema(conn)
    ref_date = _last_trading_date()

    if args.audit_only:
        tickers = _build_static_only_universe(args)
        before = classify_tickers(conn, tickers, ref_date)
        _print_audit("BEFORE", before)
        json.dump({"before": _counts(before)}, sys.stdout, indent=2)
        print(file=sys.stdout)
        return

    # Refresh mode: build the FULL scanner universe via real TrendScanConfig.
    cfg = TrendScanConfig(sp500_path=args.sp500, nasdaq100_path=args.nasdaq100)
    tickers, uw_client, ib_client_preopened = _build_triple_source_universe(cfg)

    # Persist so the 8:30 AM scan can reuse the exact same universe.
    UNIVERSE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    UNIVERSE_CACHE.write_text(json.dumps({
        "tickers": tickers,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "source_counts": {
            "total": len(tickers),
            "has_uw": uw_client is not None,
            "has_ib_scanner": ib_client_preopened is not None,
        },
    }, indent=2))

    before = classify_tickers(conn, tickers, ref_date)
    _print_audit("BEFORE", before)

    t0 = time.monotonic()
    if ib_client_preopened is not None:
        ib = ib_client_preopened
    else:
        try:
            from scripts.clients.ib_client import IBClient
            ib = IBClient()
            ib.connect(client_id="auto")
        except Exception as exc:
            logger.warning("IB connection failed: %s — skipping refresh", exc)
            json.dump({"before": _counts(before), "error": str(exc)}, sys.stdout, indent=2)
            print(file=sys.stdout)
            return

    from scripts.ta_lib.service import TAService
    ta_svc = TAService(db_path=args.db, ib_client=ib)

    refresh_tickers = tickers if args.force else before["stale"] + before["missing"]
    if refresh_tickers:
        logger.info("Refreshing %d tickers ...", len(refresh_tickers))
        ta_svc.bulk_refresh(refresh_tickers)

    elapsed = time.monotonic() - t0
    after = classify_tickers(conn, tickers, ref_date)
    _print_audit("AFTER", after)

    failed = sorted(set(after["stale"] + after["missing"]) & set(refresh_tickers))
    result = {
        "before": _counts(before),
        "after": _counts(after),
        "refreshed": len(refresh_tickers),
        "failed_tickers": failed,
        "elapsed_s": round(elapsed, 1),
    }
    json.dump(result, sys.stdout, indent=2)
    print(file=sys.stdout)

    try:
        ib.disconnect()
    except Exception:
        pass
```

- [ ] **Step 5: Run new tests + regression sweep**

Run: `python3.13 -m pytest scripts/tests/test_ta_lib/test_premarket_prep.py -xvs`
Expected: PASS — both new tests (audit-only offline guarantee + triple-source universe) green.

Run: `python3.13 -m pytest scripts/tests/test_ta_lib/ -xvs`
Expected: PASS.

- [ ] **Step 6: Manual smoke — audit-only stays offline**

Run: `python3.13 scripts/ta_premarket_prep.py --audit-only` with `UW_TOKEN=` unset.
Expected: exits 0; JSON stdout with `before` counts; no UW/IB connection attempts in logs.

- [ ] **Step 7: Commit**

```bash
git add scripts/ta_premarket_prep.py scripts/tests/test_ta_lib/test_premarket_prep.py
git commit -m "fix(ta_premarket_prep): triple-source universe in refresh mode

Scanner universe is triple-source (static + UW flow + IB scanner).
Prep was only warming static + SPY, so UW-flow-discovered or
IB-scanner-discovered tickers were cold at 8:30 AM and the scan
silently skipped them (allow_fetch=False).

Refresh mode now instantiates a real TrendScanConfig() — same
uw_flow_lookback_days=5 and uw_flow_min_premium=100_000 the scanner
uses — so prep and scan see identical universes. Persists to
data/ta_premarket_universe.json for scan-time alignment (Task 3b).

--audit-only kept strictly offline: static universe only, no UW/IB
connection attempts, existing offline tests preserved."
```

---

### Task 3b: Scanner consumes the prep-persisted universe

**Files:**

- Modify: `scripts/trend_scan.py` (`run_scan_pipeline` — accept cached universe)
- Test: `scripts/tests/test_trend_scan_e2e.py`

**Contract:** If `data/ta_premarket_universe.json` exists AND its `built_at` is within 2 hours, use its `tickers` list. Otherwise rebuild via `build_universe()`. This eliminates the duplicate triple-source fetch at 8:30 AM and guarantees prep–scan alignment.

- [ ] **Step 1: Write the failing test**

```python
def test_run_scan_pipeline_uses_fresh_universe_cache(tmp_path, monkeypatch):
    """When data/ta_premarket_universe.json exists and is <2h old, use it."""
    import json, time
    from datetime import datetime
    import scripts.trend_scan as ts

    cache = tmp_path / "ta_premarket_universe.json"
    cache.write_text(json.dumps({
        "tickers": ["AAPL", "PREP_ONLY_XYZ"],
        "built_at": datetime.now().isoformat(timespec="seconds"),
    }))
    monkeypatch.setattr(ts, "UNIVERSE_CACHE_PATH", cache, raising=False)

    # If build_universe is called we fail — cache should pre-empt it
    monkeypatch.setattr(ts, "build_universe",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("build_universe should not be called")))

    resolved = ts._resolve_universe(cfg=ts.TrendScanConfig(), uw_client=None, ib_client=None)
    assert "PREP_ONLY_XYZ" in resolved


def test_run_scan_pipeline_rebuilds_if_cache_stale(tmp_path, monkeypatch):
    """When cache is >2h old, fall back to build_universe()."""
    import json
    from datetime import datetime, timedelta
    import scripts.trend_scan as ts

    cache = tmp_path / "ta_premarket_universe.json"
    cache.write_text(json.dumps({
        "tickers": ["STALE_CACHED"],
        "built_at": (datetime.now() - timedelta(hours=3)).isoformat(timespec="seconds"),
    }))
    monkeypatch.setattr(ts, "UNIVERSE_CACHE_PATH", cache, raising=False)
    monkeypatch.setattr(ts, "build_universe", lambda cfg, **k: ["FRESH_BUILD"])

    resolved = ts._resolve_universe(cfg=ts.TrendScanConfig(), uw_client=None, ib_client=None)
    assert resolved == ["FRESH_BUILD"]
```

- [ ] **Step 2: Run test — expect failure**

Run: `python3.13 -m pytest scripts/tests/test_trend_scan_e2e.py::test_run_scan_pipeline_uses_fresh_universe_cache -xvs`
Expected: FAIL — `_resolve_universe` doesn't exist.

- [ ] **Step 3: Implement `_resolve_universe` in `scripts/trend_scan.py`**

Add near `run_scan_pipeline`:

```python
UNIVERSE_CACHE_PATH = Path(_PROJECT_ROOT) / "data" / "ta_premarket_universe.json"
UNIVERSE_CACHE_MAX_AGE_S = 2 * 60 * 60  # 2 hours


def _resolve_universe(
    *,
    cfg: TrendScanConfig,
    uw_client: Any,
    ib_client: Any,
) -> list[str]:
    """Prefer prep-persisted universe if fresh (<2h); otherwise rebuild.

    This guarantees the 8:30 AM scan sees the same universe ta_premarket_prep
    warmed at 6:00 AM — no silent mismatches from UW flow changes between
    prep and scan time."""
    try:
        if UNIVERSE_CACHE_PATH.exists():
            payload = json.loads(UNIVERSE_CACHE_PATH.read_text())
            built_at = datetime.fromisoformat(payload["built_at"])
            age_s = (datetime.now() - built_at).total_seconds()
            if age_s <= UNIVERSE_CACHE_MAX_AGE_S and payload.get("tickers"):
                logger.info("Using prep-persisted universe: %d tickers, %.0fs old",
                            len(payload["tickers"]), age_s)
                return payload["tickers"]
            else:
                logger.info("Universe cache stale (%.0fs old, max %d); rebuilding",
                            age_s, UNIVERSE_CACHE_MAX_AGE_S)
    except Exception as exc:
        logger.warning("Universe cache read failed: %s — rebuilding", exc)

    return build_universe(cfg, uw_client=uw_client, ib_client=ib_client)
```

Replace the `universe = build_universe(cfg, uw_client=uw_client, ib_client=ib_client)` line inside `run_scan_pipeline` with `universe = _resolve_universe(cfg=cfg, uw_client=uw_client, ib_client=ib_client)`.

- [ ] **Step 4: Run tests**

Run: `python3.13 -m pytest scripts/tests/test_trend_scan_e2e.py -xvs`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/trend_scan.py scripts/tests/test_trend_scan_e2e.py
git commit -m "feat(trend_scan): consume prep-persisted universe cache

Close the loop on Task 3: prep writes data/ta_premarket_universe.json
at 6 AM, scanner now reads it at 8:30 AM if fresh (<2h). Eliminates
duplicate triple-source UW/IB fetch at scan time and guarantees
prep/scan alignment. Falls back to build_universe() on cache miss
or stale cache."
```

---

# Phase 1 Checkpoint

- [ ] Run the whole TA-related test suite:
      `python3.13 -m pytest scripts/tests/test_ta_lib/ scripts/tests/test_trend_scan_runtime.py scripts/tests/test_trend_scan_e2e.py -xvs`
      Expected: all green.
- [ ] Review the three commits with `git log -3 --stat` to confirm scope is limited to the declared files.
- [ ] Human review gate before starting Phase 2.

---

# Phase 2 — Signal Accuracy

Goal: fix correctness bugs in signal logic. Each task isolated to one stage.

---

### Task 4: Extend snapshot with `high_20d`, `low_20d`, `low_52w`, `up_day_volume_ratio` (enabler for #1, #3, #9)

**Files:**

- Modify: `scripts/ta_lib/service.py:152-164` (get_snapshot)
- Test: `scripts/tests/test_ta_lib/test_service.py`
- Test: `scripts/tests/test_ta_lib/test_snapshot_contract.py`

**Rationale:** Original v1 plan split snapshot enablers across Tasks 4 (high_20d, up_day_volume_ratio) and 9 Step 4 (low_20d, low_52w). Consolidated here because they're one cohesive schema change and mock dict updates in tests only want to happen once.

- [ ] **Step 1: Write the failing contract test**

Append to `scripts/tests/test_ta_lib/test_snapshot_contract.py`:

```python
def test_snapshot_exposes_high_low_and_up_day_volume_ratio(seeded_duckdb):
    """Snapshot must expose:
      - high_20d, low_20d: for breakout / breakdown verification
      - low_52w: bearish-mirror of high_52w (for near-52w-low detection)
      - up_day_volume_ratio: volume-confirmed trend scoring

    up_day_volume_ratio = mean(up-day volume) / mean(down-day volume) over 10 sessions."""
    from scripts.ta_lib.service import TAService

    svc = TAService(db_path=seeded_duckdb, ib_client=None)
    snap = svc.get_snapshot("AAPL", allow_fetch=False)

    for field in ("high_20d", "low_20d", "low_52w", "up_day_volume_ratio"):
        assert field in snap, f"missing {field} in {sorted(snap.keys())}"

    assert snap["high_20d"] >= snap["low_20d"] > 0
    assert snap["low_52w"] > 0
    assert 0.0 < snap["up_day_volume_ratio"] < 10.0


def test_up_day_volume_ratio_is_neutral_with_insufficient_samples(tmp_path):
    """When fewer than 3 up-days OR fewer than 3 down-days are available in
    the 10-session window, up_day_volume_ratio MUST default to 1.0 (neutral).

    Post-tribunal fix: the previous 2.0 sentinel for all-up windows created
    false-precision spikes that dominated the trend score without real
    evidence (Task 6 weights this signal 2x)."""
    from scripts.ta_lib.service import TAService
    from scripts.ta_lib.store import get_connection, init_schema, write_bars
    import pandas as pd
    from datetime import date, timedelta

    db = tmp_path / "ta.duckdb"
    conn = get_connection(str(db))
    init_schema(conn)

    # Seed 10 sessions of monotonically up prices — zero down days.
    today = date.today()
    rows = []
    base = 100.0
    for i in range(10):
        d = today - timedelta(days=10 - i)
        rows.append({"date": pd.Timestamp(d), "open": base + i,
                     "high": base + i + 0.5, "low": base + i - 0.5,
                     "close": base + i + 0.3, "volume": 1_000_000})
    write_bars(conn, "ALLUP", "1d", pd.DataFrame(rows))

    # Compute indicators so _is_stale is satisfied (normally done by bulk_refresh)
    from scripts.ta_lib.indicators import compute_all_indicators
    from scripts.ta_lib.store import write_indicators
    bars_df = pd.DataFrame(rows).set_index("date")
    write_indicators(conn, "ALLUP", "1d", compute_all_indicators(bars_df))

    svc = TAService(db_path=str(db), ib_client=None)
    snap = svc.get_snapshot("ALLUP", allow_fetch=False)
    assert snap["up_day_volume_ratio"] == 1.0, (
        f"expected neutral (1.0) when sample is all-up, got {snap['up_day_volume_ratio']}"
    )
```

If the `seeded_duckdb` fixture does not exist in test_snapshot_contract.py, reuse whatever fixture the other tests in that file use (likely named `svc` or similar — read the first 40 lines of test_snapshot_contract.py to confirm the name).

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.13 -m pytest scripts/tests/test_ta_lib/test_snapshot_contract.py::test_snapshot_includes_high_20d_and_up_day_volume_ratio -xvs`
Expected: FAIL — "missing high_20d".

- [ ] **Step 3: Extend `get_snapshot()` in `scripts/ta_lib/service.py`**

Insert after line 160 (after the `range_20d_pct` block):

```python
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
        vols = recent["volume"].fillna(0)
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
```

- [ ] **Step 4: Run contract + service tests**

Run: `python3.13 -m pytest scripts/tests/test_ta_lib/test_snapshot_contract.py scripts/tests/test_ta_lib/test_service.py -xvs`
Expected: PASS.

- [ ] **Step 5: Update e2e mock dict in `scripts/tests/test_trend_scan_e2e.py:12-36`**

Add two fields to `_mock_ohlcv_data`:

```python
        "high_20d": 151 if bullish else 149,
        "up_day_volume_ratio": 1.4 if bullish else 0.7,
```

- [ ] **Step 6: Run e2e tests**

Run: `python3.13 -m pytest scripts/tests/test_trend_scan_e2e.py -xvs`
Expected: PASS (no behavior change yet — we added fields, didn't use them).

- [ ] **Step 7: Commit**

```bash
git add scripts/ta_lib/service.py scripts/tests/test_ta_lib/test_snapshot_contract.py scripts/tests/test_trend_scan_e2e.py
git commit -m "feat(ta_lib): expose high_20d/low_20d/low_52w/up_day_volume_ratio

Four schema additions enabling downstream signal fixes:
  - high_20d: bullish breakout verification (close above 20d high).
  - low_20d: bearish breakdown verification (close below 20d low).
  - low_52w: bearish mirror of high_52w.
  - up_day_volume_ratio: accumulation/distribution detector with
    neutral fallback (1.0) when sample is insufficient (<3 up days OR
    <3 down days in the 10-session window). Avoids false-precision
    spike that a naive 2.0 sentinel would cause on all-up windows.

Pure additive change — existing snapshot consumers untouched."
```

---

### Task 5: Breakout detection verifies price actually breaks out (Finding #3)

**Files:**

- Modify: `scripts/trend_scan_lib/stages/ta_prefilter.py:113-117` (detect_breakout signature)
- Modify: `scripts/trend_scan_lib/stages/ta_prefilter.py:145-181` (compute_trend_score — the **only** real caller)
- Modify: existing tests in `scripts/tests/test_ta_prefilter.py:114-128` (3 existing `detect_breakout` tests use old signature — they WILL break and must be updated in the same commit)
- Test: `scripts/tests/test_ta_prefilter.py`

**Tribunal note:** v1 incorrectly claimed call sites live in `scripts/trend_scan.py`. The only real call is inside `compute_trend_score()` in `ta_prefilter.py` at line 173. The 3 existing `detect_breakout` tests at `test_ta_prefilter.py:114-128` pass the old 4-arg signature and will become red the moment we change the signature — this is expected and handled in Step 5.

- [ ] **Step 1: Locate existing detect_breakout tests**

Run: `grep -n "detect_breakout" scripts/tests/test_ta_prefilter.py`
Expected: hits at lines ~114-128 using the old `(close, high_52w, range_20d_pct, atr_pct)` signature.

- [ ] **Step 2: Write the failing test**

```python
def test_detect_breakout_requires_close_above_20d_high():
    """A narrow-range stock NOT above its 20d high must not register
    as a breakout. Consolidation alone is not a breakout signal — the
    breakout itself must be occurring."""
    from scripts.trend_scan_lib.stages.ta_prefilter import detect_breakout

    # Narrow consolidation, but close is mid-range, not above 20d high
    result = detect_breakout(
        close=100.0,
        high_52w=120.0,        # not near 52w high
        high_20d=105.0,        # 20d high is 5% above close
        range_20d_pct=0.03,    # tight range
        atr_pct=0.02,          # range_20d_pct < atr_pct * 3 (0.06)
    )
    assert result is False, "consolidation without breakout must not register"

    # Same narrow consolidation, close now ABOVE 20d high
    result = detect_breakout(
        close=106.0,
        high_52w=120.0,
        high_20d=105.0,
        range_20d_pct=0.03,
        atr_pct=0.02,
    )
    assert result is True, "close above 20d high in tight range = breakout"


def test_detect_breakout_near_52w_high_still_qualifies():
    """Pre-existing path: within 3% of 52w high always qualifies as breakout
    regardless of 20d structure."""
    from scripts.trend_scan_lib.stages.ta_prefilter import detect_breakout

    result = detect_breakout(
        close=119.0,
        high_52w=120.0,
        high_20d=119.5,
        range_20d_pct=0.08,
        atr_pct=0.02,
    )
    assert result is True
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3.13 -m pytest scripts/tests/test_trend_prefilter.py -xvs`
Expected: FAIL — `detect_breakout() got an unexpected keyword argument 'high_20d'`.

- [ ] **Step 4: Update detect_breakout signature and logic**

Replace `scripts/trend_scan_lib/stages/ta_prefilter.py:113-117`:

```python
def detect_breakout(
    *,
    close: float,
    high_52w: float,
    high_20d: float,
    range_20d_pct: float,
    atr_pct: float,
) -> bool:
    """Detect breakout.

    Two qualifying paths:
      1. Within 3% of 52w high — price is punching through long-term resistance.
      2. Close is above 20d high AND the 20d range was tight — coiled spring release.

    Previous version accepted path 2 on consolidation narrowness alone,
    which flagged stocks sitting mid-range in a tight band as 'breakouts'."""
    near_52w = high_52w > 0 and (high_52w - close) / high_52w <= 0.03
    tight_range = atr_pct > 0 and range_20d_pct < atr_pct * 3
    above_20d_high = high_20d > 0 and close >= high_20d
    consolidation_break = tight_range and above_20d_high
    return near_52w or consolidation_break
```

- [ ] **Step 5: Update the real call site in `compute_trend_score()` AND update the 3 pre-existing tests**

Replace `scripts/trend_scan_lib/stages/ta_prefilter.py:173-178` (the `if detect_breakout(...)` block inside `compute_trend_score`) with:

```python
    if detect_breakout(
        close=indicators["close"],
        high_52w=indicators.get("high_52w", 0),
        high_20d=indicators.get("high_20d", 0),
        range_20d_pct=indicators.get("range_20d_pct", 1.0),
        atr_pct=indicators.get("atr_pct", 0),
    ):
        composite += BREAKOUT_BONUS
```

Update the 3 pre-existing tests in `scripts/tests/test_ta_prefilter.py`:

```python
# Line ~114-116 BEFORE:
assert detect_breakout(close=148, high_52w=150, range_20d_pct=0.05, atr_pct=0.02) is True
# AFTER (near-52w path, high_20d irrelevant — set to anything > 0):
assert detect_breakout(close=148, high_52w=150, high_20d=147.5, range_20d_pct=0.05, atr_pct=0.02) is True

# Line ~120-122 BEFORE:
assert detect_breakout(close=100, high_52w=120, range_20d_pct=0.03, atr_pct=0.015) is True
# AFTER (consolidation path — now requires close >= high_20d. Use high_20d=99.5):
assert detect_breakout(close=100, high_52w=120, high_20d=99.5, range_20d_pct=0.03, atr_pct=0.015) is True

# Line ~126-128 BEFORE:
assert detect_breakout(close=100, high_52w=150, range_20d_pct=0.15, atr_pct=0.02) is False
# AFTER (neither path qualifies):
assert detect_breakout(close=100, high_52w=150, high_20d=105, range_20d_pct=0.15, atr_pct=0.02) is False
```

Grep once more to confirm no other callers: `grep -rn "detect_breakout(" scripts/ --include="*.py"` — the only hits should be the updated `compute_trend_score` and the test file.

- [ ] **Step 6: Run prefilter + e2e tests**

Run: `python3.13 -m pytest scripts/tests/test_trend_prefilter.py scripts/tests/test_trend_scan_e2e.py -xvs`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add scripts/trend_scan_lib/stages/ta_prefilter.py scripts/trend_scan.py scripts/tests/test_trend_prefilter.py
git commit -m "fix(trend_scan): breakout requires price above 20d high, not just tight range

detect_breakout() previously returned True on consolidation narrowness
alone (range_20d_pct < atr_pct * 3), flagging any mid-range stock in a
tight band as a breakout. Now the consolidation path also requires
close >= high_20d — the spring must actually release."
```

---

### Task 6: Volume profile isolates up-day volume (Finding #9)

**Files:**

- Modify: `scripts/trend_scan_lib/stages/ta_prefilter.py` (`score_volume_profile` is defined here — NOT in volatility.py; v1 plan targeted the wrong module)
- Modify: `scripts/trend_scan_lib/stages/ta_prefilter.py:163-167` (`compute_trend_score` — the only real caller)
- Test: `scripts/tests/test_ta_prefilter.py:95-98` (extend existing `test_score_volume_profile_above_avg`)

**Tribunal note:** v1 plan said "Modify scripts/trend_scan_lib/stages/volatility.py:90-97" but `score_volume_profile` lives in `ta_prefilter.py` and test_ta_prefilter.py imports it from there. There is no `score_volume_profile` in `volatility.py`.

- [ ] **Step 1: Confirm file locations**

Run: `grep -n "def score_volume_profile\|score_volume_profile(" scripts/trend_scan_lib/stages/*.py scripts/tests/`
Expected: definition in `ta_prefilter.py`; one internal call from `compute_trend_score`; test references in `test_ta_prefilter.py`.

- [ ] **Step 2: Write failing test**

Append to `scripts/tests/test_ta_prefilter.py`:

```python
def test_score_volume_profile_penalizes_distribution():
    """Stock rallying on low volume while selling on high volume (distribution)
    must score lower than one accumulating (high volume on up days)."""
    from scripts.trend_scan_lib.stages.ta_prefilter import score_volume_profile

    accumulation = score_volume_profile(
        recent_avg_volume=1_500_000,
        avg_20d_volume=1_000_000,
        recent_up_ratio=0.7,
        up_day_volume_ratio=1.5,
    )
    distribution = score_volume_profile(
        recent_avg_volume=1_500_000,
        avg_20d_volume=1_000_000,
        recent_up_ratio=0.7,
        up_day_volume_ratio=0.6,
    )
    assert accumulation > distribution, (
        f"accumulation ({accumulation}) should outscore distribution ({distribution})"
    )


def test_score_volume_profile_neutral_when_ratio_missing():
    """When up_day_volume_ratio is 1.0 (neutral sentinel), score stays near legacy level."""
    from scripts.trend_scan_lib.stages.ta_prefilter import score_volume_profile

    score = score_volume_profile(
        recent_avg_volume=1_500_000,
        avg_20d_volume=1_000_000,
        recent_up_ratio=0.7,
        up_day_volume_ratio=1.0,
    )
    assert 0.3 < score < 0.9, f"neutral score should be mid-range, got {score}"
```

- [ ] **Step 3: Run test — expect failure**

Run: `python3.13 -m pytest scripts/tests/test_ta_prefilter.py -k volume_profile -xvs`
Expected: FAIL — `unexpected keyword argument 'up_day_volume_ratio'`.

- [ ] **Step 4: Update `score_volume_profile` in `ta_prefilter.py`**

Locate the current definition (around line 90 of ta_prefilter.py based on v1's bad line reference — grep to confirm):

```python
def score_volume_profile(
    *,
    recent_avg_volume: float,
    avg_20d_volume: float,
    recent_up_ratio: float,
    up_day_volume_ratio: float = 1.0,
) -> float:
    """Score volume profile. Three signals, last one weighted 2x:
      - Volume pickup (recent vs 20d) — trend attention.
      - Up-day frequency (recent_up_ratio) — directional bias.
      - Up-day vs down-day volume (up_day_volume_ratio) — accumulation vs distribution.
    """
    if avg_20d_volume == 0:
        return 0.5
    vol_ratio = recent_avg_volume / avg_20d_volume
    vol_score = normalize_score(vol_ratio - 0.5)
    up_score = normalize_score(recent_up_ratio * 1.5 - 0.25)
    # up_day_volume_ratio typically 0.3–2.5; 1.0 = neutral, 1.5+ = accumulation, 0.7- = distribution.
    accumulation_score = normalize_score((up_day_volume_ratio - 0.7) / 1.0)
    return (vol_score + up_score + 2 * accumulation_score) / 4
```

- [ ] **Step 5: Thread `up_day_volume_ratio` through the real caller**

Replace the block at `scripts/trend_scan_lib/stages/ta_prefilter.py:163-167` (the call inside `compute_trend_score`):

```python
        "volume_profile": score_volume_profile(
            recent_avg_volume=indicators.get("recent_avg_volume", 0),
            avg_20d_volume=indicators.get("avg_20d_volume", 1),
            recent_up_ratio=indicators.get("recent_up_ratio", 0.5),
            up_day_volume_ratio=indicators.get("up_day_volume_ratio", 1.0),
        ),
```

Note: the local dict parameter inside `compute_trend_score` is named `indicators`, not `ohlcv` or `snapshot` — don't blindly rename. The scanner's snapshot dict is passed INTO `compute_trend_score(indicators=snapshot)` from `_stage_a`.

- [ ] **Step 6: Run tests**

Run: `python3.13 -m pytest scripts/tests/test_trend_prefilter.py scripts/tests/test_trend_scan_e2e.py -xvs`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add scripts/trend_scan_lib/stages/ta_prefilter.py scripts/tests/test_ta_prefilter.py
git commit -m "fix(trend_scan): volume profile isolates up-day vs down-day volume

Prior score_volume_profile averaged recent/20d volume and up-day price
ratio. That gave a high score to any stock rallying, even on distributive
volume (low vol on up days, high vol on down days — classic accumulation
inversion).

Snapshot now exposes up_day_volume_ratio; scoring weights it 2× so
distribution patterns actually penalize the trend score."
```

---

### Task 7: Stage B overhead-wall reject (Finding #8)

**Files:**

- Modify: `scripts/trend_scan_lib/stages/options_structure.py:90-125`
- Test: existing test for options_structure OR new

- [ ] **Step 1: Locate existing tests**

Run: `grep -rn "compute_structure_score\|is_severely_pinned" scripts/tests/`

- [ ] **Step 2: Write failing test**

```python
def test_compute_structure_score_rejects_overhead_wall_without_support():
    """A large call wall within 2% above spot with no put wall below
    = immediate overhead resistance with nothing to bounce off.
    Must hard-reject like pinning does."""
    from scripts.trend_scan_lib.stages.options_structure import compute_structure_score

    score, rejected = compute_structure_score({
        "spot": 100.0,
        "max_pain": 95.0,          # not pinned
        "gex_at_spot": 0.0,
        "call_wall": 101.5,        # within 2% above spot
        "put_wall": 0.0,           # no support below
        "net_call_oi_change": 0,
        "net_put_oi_change": 0,
        "net_gex": 0,
        "gamma_flip": 95.0,
    })

    assert rejected is True, "overhead wall with no put support must reject"
    assert score == 0.0


def test_compute_structure_score_overhead_wall_ok_with_put_support():
    """Overhead wall is acceptable if a meaningful put wall exists below —
    range-bound structure is still tradeable."""
    from scripts.trend_scan_lib.stages.options_structure import compute_structure_score

    score, rejected = compute_structure_score({
        "spot": 100.0,
        "max_pain": 95.0,
        "gex_at_spot": 0.0,
        "call_wall": 101.5,
        "put_wall": 98.0,          # meaningful support
        "net_call_oi_change": 0,
        "net_put_oi_change": 1000,
        "net_gex": 0,
        "gamma_flip": 98.0,
    })

    assert rejected is False
```

- [ ] **Step 3: Run test to verify failure**

Run: `python3.13 -m pytest scripts/tests/ -k structure_score -xvs`
Expected: FAIL — overhead wall case is not rejected.

- [ ] **Step 4: Implement overhead-wall check**

In `scripts/trend_scan_lib/stages/options_structure.py`, add after the pinning check (around line 101):

```python
OVERHEAD_WALL_PCT_ABOVE = 0.02  # call wall within 2% above spot
SUPPORTIVE_PUT_PCT_BELOW = 0.03  # put wall within 3% below spot counts as support


def has_unsupported_overhead_wall(
    *,
    spot: float,
    call_wall: float,
    put_wall: float,
) -> bool:
    """True iff a call wall sits close above spot with no meaningful put
    wall below. This is the second hard-fail case in Stage B structure
    (the first being severe pinning)."""
    if spot <= 0 or call_wall <= 0:
        return False
    call_overhead = (call_wall - spot) / spot
    if not (0 < call_overhead <= OVERHEAD_WALL_PCT_ABOVE):
        return False
    # Check for supportive put wall
    if put_wall > 0:
        put_support = (spot - put_wall) / spot
        if 0 < put_support <= SUPPORTIVE_PUT_PCT_BELOW:
            return False  # supported — not a hard reject
    return True
```

Then in `compute_structure_score`, insert a **single new block** immediately after the existing `is_severely_pinned` check at line 109. Do not touch any other line; the remaining scoring logic is preserved verbatim.

The diff (using Edit tool or manual insert):

```python
# BEFORE (options_structure.py:104-110, unchanged):
def compute_structure_score(data: dict) -> tuple[float, bool]:
    spot = data.get("spot", 0)
    max_pain = data.get("max_pain", 0)
    gex_at_spot = data.get("gex_at_spot", 0)

    if is_severely_pinned(spot=spot, max_pain=max_pain, gex_at_spot=gex_at_spot):
        return 0.0, True

# INSERT these 6 lines immediately after line 110:
    if has_unsupported_overhead_wall(
        spot=spot,
        call_wall=data.get("call_wall", 0),
        put_wall=data.get("put_wall", 0),
    ):
        return 0.0, True

# AFTER (line 111 onwards): existing scoring logic is untouched.
# Do not modify any lines below the inserted block.
```

**Note:** Task 9 Step 6 will later rewrite `compute_structure_score` end-to-end to add bearish-direction support. That rewrite preserves the overhead-wall check added here, so this is a correct forward-compatible step.

- [ ] **Step 5: Run tests**

Run: `python3.13 -m pytest scripts/tests/ -k structure -xvs`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/trend_scan_lib/stages/options_structure.py scripts/tests/
git commit -m "fix(trend_scan): Stage B rejects unsupported overhead walls

Spec requires two hard-fail conditions in Stage B structure scoring:
  1. Severe pinning (already implemented)
  2. Large call wall within 2% above spot with no supportive put wall

Case 2 was missing — a ticker with an immediate overhead wall could
still pass Stage B with a reduced score. Now rejected like pinning."
```

---

# Phase 2 Checkpoint

- [ ] Full test sweep:
      `python3.13 -m pytest scripts/tests/test_ta_lib/ scripts/tests/test_trend_prefilter.py scripts/tests/test_trend_scan_e2e.py scripts/tests/test_trend_scan_runtime.py -xvs`
      Expected: all green.
- [ ] Smoke-run the scan with cached data:
      `python3.13 scripts/trend_scan.py --top 10`
      Expected: exits 0; candidate count may differ from the pre-fix run (likely fewer due to stricter breakout + overhead-wall rejects).
- [ ] Visually diff candidate list against previous run — any ticker that dropped should be one with consolidation-without-breakout or overhead-wall structure. Any ticker that rose should have real up-day volume.
- [ ] Human review gate before Phase 3.

---

# Phase 3 — Scope Expansion

Three large changes. Do them in this order — Task 8 is scoping cleanup that Task 9 and 10 depend on.

---

### Task 8: Strip `suggested_trade`, add analysis-only flag (Finding #2)

**Files (blast radius — tribunal-expanded):**

- Modify: `scripts/trend_scan_lib/models.py:11-34` (`TrendCandidate`)
- Modify: `scripts/trend_scan.py` (remove trade-type wiring, add structure_hint; **keep `scores=scores`**)
- Modify: `scripts/trend_scan_lib/stages/volatility.py` (remove `suggest_trade_type` import at `scripts/trend_scan.py:30` and its call site at `scripts/trend_scan.py:416`; leave the function itself present but unused so `test_volatility.py:87-101` tests still pass until a later cleanup)
- **Modify: `scripts/trend_scan_lib/storage.py`** — DuckDB schema drops `suggested_trade` column, adds `structure_hint` + `catalysts` (JSON string). Update `init_schema()` and `write_scan_candidates()`.
- **Modify: `scripts/tests/test_trend_storage.py`** — column assertions.
- **Modify: `web/lib/types.ts`** — `TrendCandidate` TS interface: remove `suggested_trade`, add `structure_hint`, `catalysts: string[]`, `flags: string[]`.
- **Modify: `web/components/WorkspaceSections.tsx`** — any render of `candidate.suggested_trade` becomes `candidate.structure_hint` (+ a small "analysis only" badge if it surfaces the `four_gates_not_applied` flag).
- Test: `scripts/tests/test_trend_models.py`, `scripts/tests/test_trend_scan_e2e.py`, `scripts/tests/test_trend_storage.py`, `web/tests/*.test.ts` wherever TrendCandidate surfaces.

**Tribunal note:** v1 plan said "API/server — no change expected" which was wrong. Removing `suggested_trade` from the model without updating storage/web layers produces write-time errors (schema mismatch) and render-time undefineds. This task is now a 3-layer coordinated change.

- [ ] **Step 1: Write failing test for the new contract**

Replace or augment relevant tests in `scripts/tests/test_trend_models.py`:

```python
def test_trend_candidate_has_four_gates_flag_and_no_suggested_trade():
    """Scanner is analysis-only. Output carries a flag making that explicit,
    and must not emit a 'suggested_trade' field (would invite consumers to
    trade without running Four Gates)."""
    from scripts.trend_scan_lib.models import TrendCandidate

    c = TrendCandidate(
        ticker="AAPL", direction="bullish", final_score=0.8,
        spot_price=150.0, indicators={}, structure_hint="long_call_spread",
    )
    d = c.to_dict()
    assert "suggested_trade" not in d
    assert d["structure_hint"] == "long_call_spread"
    assert "four_gates_not_applied" in d["flags"]
```

- [ ] **Step 2: Run test to verify failure**

Run: `python3.13 -m pytest scripts/tests/test_trend_models.py -xvs`
Expected: FAIL — `suggested_trade` still present, `structure_hint` missing.

- [ ] **Step 3: Update `TrendCandidate`**

Replace `scripts/trend_scan_lib/models.py:11-34`:

```python
@dataclass
class TrendCandidate(BaseScanCandidate):
    """A ranked trend scan candidate.

    ANALYSIS-ONLY: this object describes signal, not a trade. The
    `structure_hint` field suggests an options structure that *might*
    fit the signal's convexity profile, but Four Gates (convexity
    arithmetic, edge validation, Kelly sizing, no-naked-shorts) are NOT
    applied here — that happens at order-routing time.

    The 'four_gates_not_applied' flag is auto-added to every candidate
    so downstream consumers cannot miss this."""

    spot_price: float = 0.0
    indicators: dict[str, float] = field(default_factory=dict)
    structure_hint: str = ""           # informational only
    invalidation: float = 0.0
    holding_window: str = "5-15 trading days"
    catalysts: list[str] = field(default_factory=list)  # populated by Stage C (Task 10)

    def __post_init__(self):
        if "four_gates_not_applied" not in self.flags:
            self.flags.append("four_gates_not_applied")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "direction": self.direction,
            "final_score": self.final_score,
            "scores": self.scores,
            "spot_price": self.spot_price,
            "indicators": self.indicators,
            "summaries": self.summaries,
            "structure_hint": self.structure_hint,
            "invalidation": self.invalidation,
            "flags": self.flags,
            "holding_window": self.holding_window,
            "catalysts": self.catalysts,
        }
```

Note: if `BaseScanCandidate` defines `__post_init__`, call super. Run `grep -n "class BaseScanCandidate" scripts/scanner_lib/` first to check.

- [ ] **Step 4: Remove `suggest_trade_type` call in `volatility.py`**

Delete lines 54-63 of `scripts/trend_scan_lib/stages/volatility.py` (the `suggest_trade_type` function) OR keep the function but stop calling it from the scanner.

- [ ] **Step 5: Update `scripts/trend_scan.py`**

At the `TrendCandidate` construction site (around line 551), replace `suggested_trade=...` with `structure_hint=...` and source the hint from existing structure/vol analysis (no new trade-structure logic):

At `scripts/trend_scan.py:551` (inside `run_scan_pipeline`'s existing for-loop over `stage_bc_pairs`), replace the candidate construction block:

```python
    direction = "bullish"  # temporary — Task 9 makes this dynamic via two-pass loop
    candidate = TrendCandidate(
        ticker=ticker,
        direction=direction,
        final_score=compute_final_score(scores, cfg.weights),
        scores=scores,                               # <-- RESTORED (v1 dropped this; TypeError)
        spot_price=ohlcv.get("close", 0),
        indicators={
            "ma_20": ohlcv.get("ma_20", 0),
            "ma_50": ohlcv.get("ma_50", 0),
            "ma_200": ohlcv.get("ma_200", 0),
            "rsi": ohlcv.get("rsi", 0),
            "adx": ohlcv.get("adx", 0),
            "macd_histogram": ohlcv.get("macd_histogram", 0),
            "bbw": ohlcv.get("bbw", 0),
            "rs_vs_spy": ohlcv.get("rs_vs_spy", 0),
            "iv_rank": bc["vol_data"].get("iv_rank", 0),
            "gamma_flip": bc["struct_data"].get("gamma_flip", 0),
            "call_wall": bc["struct_data"].get("call_wall", 0),
            "put_wall": bc["struct_data"].get("put_wall", 0),
        },
        summaries={
            "trend": _trend_summary(ohlcv),
            "structure": _structure_summary(bc["struct_data"]),
            "vol": _vol_summary(bc["vol_data"]),
            "flow": _flow_summary(bc["flow_data"]),
        },
        structure_hint=_infer_structure_hint(direction, bc, ohlcv),
        invalidation=_compute_invalidation(direction, ohlcv),
    )
```

**`bc` scope:** `bc` is already the loop variable from `for ticker, bc in stage_bc_pairs:` at trend_scan.py:539 — it's in scope. Similarly `ohlcv = stage_a_results[ticker]` is already fetched at line 543.

Also delete the `suggest_trade_type` call at `scripts/trend_scan.py:416-420` and remove `"suggested_trade": trade_type` from the dict returned by `_stage_bc` at line 426. The import at line 30 becomes `from scripts.trend_scan_lib.stages.volatility import compute_vol_score` (drop `suggest_trade_type`).

Add these two module-level helpers in `scripts/trend_scan.py` (above `run_scan_pipeline`):

```python
def _infer_structure_hint(direction: str, bc: dict, ohlcv: dict) -> str:
    """Return a defined-risk long-side structure hint.

    Never emits short premium — that would fail Gate 4 (naked short cover)
    if taken literally at order-entry time. Hint is informational only;
    actual structure selection happens at order-build time under Four Gates."""
    iv_rank = bc.get("vol_data", {}).get("iv_rank", 0.5)
    high_iv = iv_rank >= 0.6
    if direction == "bullish":
        return "long_call_vertical" if high_iv else "long_call"
    if direction == "bearish":
        return "long_put_vertical" if high_iv else "long_put"
    return ""


def _compute_invalidation(direction: str, ohlcv: dict) -> float:
    """Price level at which the signal is invalidated. 20DMA for both
    directions (bullish: close below = trend broken; bearish: close above
    = thesis broken)."""
    return float(ohlcv.get("ma_20", 0.0))
```

**Storage + web sub-tasks (same commit, same task):**

- [ ] **Step 5a: Update `scripts/trend_scan_lib/storage.py`** — modify `init_schema()` to drop `suggested_trade` column, add `structure_hint VARCHAR` and `catalysts VARCHAR` (JSON string). Update `write_scan_candidates()` row builder: `json.dumps(candidate.catalysts)`. Add a small migration helper if the existing `trend_scan.duckdb` has historical rows — either rename `suggested_trade` column to `structure_hint` via `ALTER TABLE` or recreate the table.
- [ ] **Step 5b: Update `scripts/tests/test_trend_storage.py`** — column list assertions.
- [ ] **Step 5c: Update `web/lib/types.ts`** — `interface TrendCandidate { ... structure_hint: string; catalysts: string[]; flags: string[]; ... }` (drop `suggested_trade`).
- [ ] **Step 5d: Update `web/components/WorkspaceSections.tsx`** — any `candidate.suggested_trade` → `candidate.structure_hint`. If the component currently renders the string as prescriptive ("suggested: call_spread"), rephrase as advisory ("structure hint: long call spread — analysis only").
- [ ] **Step 5e: Grep wider:** `grep -rn "suggested_trade" web/ scripts/` — chase every lingering reference. Update `web/tests/*.test.ts` fixtures and `data/trend_scan.json` (nuke the file; scanner will regenerate).

- [ ] **Step 6: Run full suite (Python + web)**

Run: `python3.13 -m pytest scripts/tests/test_trend_models.py scripts/tests/test_trend_scan_e2e.py scripts/tests/test_trend_storage.py -xvs`
Expected: PASS.

Run: `cd web && npm test` — ensure TS type change doesn't break web tests.
Expected: PASS.

- [ ] **Step 7: Manual smoke — full pipeline**

Delete stale scan cache: `rm -f data/trend_scan.json data/trend_scan.duckdb`.
Run: `python3.13 scripts/trend_scan.py --top 5`
Expected: exits 0; `data/trend_scan.json` has `structure_hint` (not `suggested_trade`), every candidate has `flags` containing `"four_gates_not_applied"`.

- [ ] **Step 8: Commit (single atomic cross-layer commit)**

```bash
git add scripts/trend_scan_lib/models.py scripts/trend_scan_lib/storage.py scripts/trend_scan.py scripts/tests/test_trend_models.py scripts/tests/test_trend_scan_e2e.py scripts/tests/test_trend_storage.py web/lib/types.ts web/components/WorkspaceSections.tsx
git commit -m "refactor(trend_scan): analysis-only scoping — drop suggested_trade

CLAUDE.md Four Gates are mandatory for trading decisions and live in
the order-routing layer. Scanner had been emitting 'premium_sell' /
'call_spread' suggestions via suggest_trade_type() with no gate checks
— technically a gate bypass even without execution.

Coordinated cross-layer change:
  - models.py: TrendCandidate drops suggested_trade, adds structure_hint
    (long-side defined-risk only) + catalysts + auto-flags
    'four_gates_not_applied' via __post_init__.
  - storage.py: DuckDB schema drops suggested_trade column, adds
    structure_hint + catalysts (JSON string).
  - trend_scan.py: removes suggest_trade_type call in _stage_bc;
    constructor passes scores= (was dropped in earlier draft — TypeError).
  - web/lib/types.ts + WorkspaceSections.tsx: TS types and rendering
    aligned to the new shape; prescriptive 'suggested' label becomes
    advisory 'structure hint — analysis only'.

Downstream consumers cannot misread scanner output as a trade
instruction; Gate 4 remains enforced at order build time."
```

---

### Task 9: Bearish pipeline + mirrored scoring (Finding #1)

**Files (tribunal-expanded — Stage A refactor is required):**

- Modify: `scripts/trend_scan_lib/stages/ta_prefilter.py` — add `passes_bearish_gate`, `detect_breakdown`; make `compute_trend_score` direction-aware so BREAKOUT_BONUS applies correctly to bearish candidates via `detect_breakdown`.
- Modify: `scripts/trend_scan.py` — split `_stage_a` into neutral data fetch + direction evaluation; `_stage_bc` becomes direction-aware; scanner loop emits one candidate per direction that passes.
- Modify: `scripts/trend_scan_lib/stages/options_structure.py` — `compute_structure_score(data, *, direction)` — preserve the existing 4-component `STRUCTURE_WEIGHTS` composition; branch only the wall-support component on direction.
- Test: new `scripts/tests/test_trend_scan_bearish.py`
- Test: extend `scripts/tests/test_options_structure.py` with direction-specific cases.

**Tribunal note:** v1 plan glossed over three things that make this task much bigger than advertised:

1. **Stage A hard-gates bullish.** Today `_stage_a()` returns `None` when `passes_bullish_gate()` fails. A bearish candidate never reaches the loop. The fix is to split Stage A into `_stage_a_data()` (pure fetch, no gate) + `_stage_a_direction(ohlcv, direction, cfg)` (gate + breakout/breakdown detection), then iterate over both directions in the pipeline.
2. **`compute_structure_score` currently composes 4 weighted sub-scores** (`score_gamma_flip`, `score_net_gex`, `score_max_pain`, `score_oi_change`) via the module-level `STRUCTURE_WEIGHTS` dict. A full rewrite discards this architecture. The correct fix is to branch only the wall-support sub-component on direction.
3. **Flow scoring is bullish-only** (`fetch_flow()` filters call-side alerts; `compute_flow_score` rewards positive delta/vega). A "direction-aware flow" flag is required; handled in Task 9b below to keep the task commits small.

- [ ] **Step 1: Write failing bearish-gate test**

Create `scripts/tests/test_trend_scan_bearish.py`:

```python
"""Bearish pipeline tests — mirrored scoring against the bullish gate."""

from unittest.mock import MagicMock


def test_passes_bearish_gate_mirrors_bullish_logic():
    from scripts.trend_scan_lib.stages.ta_prefilter import passes_bearish_gate

    # Clearly bearish: close below MA20, weak RSI, liquid
    assert passes_bearish_gate(
        close=95.0, ma_20=100.0, rsi=35.0,
        dollar_volume=50_000_000, min_dollar_volume=10_000_000,
    )

    # Rejects on RSI too high (not actually weak)
    assert not passes_bearish_gate(
        close=95.0, ma_20=100.0, rsi=65.0,
        dollar_volume=50_000_000, min_dollar_volume=10_000_000,
    )


def test_detect_breakdown_mirrors_breakout():
    from scripts.trend_scan_lib.stages.ta_prefilter import detect_breakdown

    # Close below 20d low with tight consolidation = breakdown
    assert detect_breakdown(
        close=94.0, low_52w=90.0, low_20d=95.0,
        range_20d_pct=0.03, atr_pct=0.02,
    )
    # Not near 52w low, not below 20d low = no breakdown
    assert not detect_breakdown(
        close=100.0, low_52w=80.0, low_20d=95.0,
        range_20d_pct=0.08, atr_pct=0.02,
    )


def test_scan_emits_both_directions_when_universe_has_both(monkeypatch):
    """Feed the scanner one bullish and one bearish mock ticker via a fake
    DataFetcher; both must appear in output with correct direction labels.

    Uses the REAL run_scan_pipeline signature:
      run_scan_pipeline(cfg, *, data_fetcher, uw_client, ib_client, db_path, ...)
    """
    from scripts.trend_scan import run_scan_pipeline
    from scripts.trend_scan_lib.config import TrendScanConfig

    # Ticker-shape mock OHLCV — must have ALL fields used by the pipeline.
    def mock_ohlcv(ticker, bullish):
        base = 150 if bullish else 95
        return {
            "ticker": ticker,
            "close": base,
            "ma_20": base - 5 if bullish else base + 5,
            "ma_50": base - 10 if bullish else base + 10,
            "ma_200": base - 20 if bullish else base + 20,
            "rsi": 62 if bullish else 35,
            "adx": 32,
            "macd": 1.5 if bullish else -1.5,
            "macd_signal": 1.0 if bullish else -1.0,
            "macd_histogram": 0.5 if bullish else -0.5,
            "rs_vs_spy": 1.15 if bullish else 0.85,
            "ma_20_series": [base - i for i in range(5)] if bullish else [base + i for i in range(5)],
            "recent_avg_volume": 1_500_000,
            "avg_20d_volume": 1_000_000,
            "recent_up_ratio": 0.7 if bullish else 0.3,
            "up_day_volume_ratio": 1.3 if bullish else 0.7,
            "bbw": 0.05,
            "high_52w": 152 if bullish else 120,
            "high_20d": 151 if bullish else 102,
            "low_20d": 140 if bullish else 95,
            "low_52w": 130 if bullish else 80,
            "range_20d_pct": 0.04,
            "atr_pct": 0.015,
            "dollar_volume": 20_000_000,
            "market_cap": 2_000_000_000,
            "price": base,
        }

    class FakeDataFetcher:
        def fetch_ohlcv(self, ticker):
            return mock_ohlcv(ticker, bullish=(ticker == "BULL"))
        def fetch_structure(self, ticker):
            bullish = ticker == "BULL"
            return {
                "spot": 150 if bullish else 95,
                "max_pain": 148 if bullish else 97,
                "gex_at_spot": 0,
                "gamma_flip": 145 if bullish else 97,
                "net_gex": 1e9,
                "call_wall": 160 if bullish else 97,
                "put_wall": 145 if bullish else 85,
                "net_call_oi_change": 5000 if bullish else -500,
                "net_put_oi_change": -500 if bullish else 5000,
            }
        def fetch_volatility(self, ticker):
            return {"iv_rank": 45, "term_structure": "normal", "earnings_days": 30}
        def fetch_flow(self, ticker):
            return {
                "ask_dominance": 0.7 if ticker == "BULL" else 0.3,
                "flow_count": 20, "expiry_cluster_ratio": 0.6,
                "avg_strike_pct_otm": 0.05, "net_delta": 1e6 if ticker == "BULL" else -1e6,
                "net_vega": 5e5, "dp_direction": "bullish" if ticker == "BULL" else "bearish",
            }

    import scripts.trend_scan as ts
    monkeypatch.setattr(ts, "build_universe", lambda cfg, **k: ["BULL", "BEAR"])

    cfg = TrendScanConfig(top_n=10)
    result = run_scan_pipeline(
        cfg, data_fetcher=FakeDataFetcher(),
        uw_client=None, ib_client=None, db_path=":memory:",
    )

    candidates = result["candidates"]
    directions = {c["direction"] for c in candidates}
    assert directions == {"bullish", "bearish"}, (
        f"expected both directions, got {directions} from {len(candidates)} candidates"
    )
    bull_cand = next(c for c in candidates if c["direction"] == "bullish")
    bear_cand = next(c for c in candidates if c["direction"] == "bearish")
    assert bull_cand["ticker"] == "BULL"
    assert bear_cand["ticker"] == "BEAR"
```

- [ ] **Step 2: Run test to verify failures**

Run: `python3.13 -m pytest scripts/tests/test_trend_scan_bearish.py -xvs`
Expected: multiple FAILs.

- [ ] **Step 3: Add bearish gate + breakdown detection**

Append to `scripts/trend_scan_lib/stages/ta_prefilter.py`:

```python
def passes_bearish_gate(
    *,
    close: float,
    ma_20: float,
    rsi: float,
    dollar_volume: float,
    min_dollar_volume: float,
) -> bool:
    """Mirror of passes_bullish_gate: close < 20DMA, RSI < 60, liquid.

    RSI < 60 (not < 40) is the symmetric threshold — the bullish gate
    requires RSI > 40 (not > 60), so the bearish cutoff at the opposite
    end of the same band preserves symmetry without double-counting the
    middle zone."""
    return close < ma_20 and rsi < 60 and dollar_volume >= min_dollar_volume


def detect_breakdown(
    *,
    close: float,
    low_52w: float,
    low_20d: float,
    range_20d_pct: float,
    atr_pct: float,
) -> bool:
    """Mirror of detect_breakout. Close near 52w low OR close below 20d
    low in a tight consolidation = breakdown."""
    near_52w = low_52w > 0 and (close - low_52w) / low_52w <= 0.03
    tight_range = atr_pct > 0 and range_20d_pct < atr_pct * 3
    below_20d_low = low_20d > 0 and close <= low_20d
    consolidation_break = tight_range and below_20d_low
    return near_52w or consolidation_break
```

- [ ] **Step 4: Snapshot low_20d / low_52w**

Already added in Task 4 (consolidated enabler). Skip to Step 5.

- [ ] **Step 5: Make `compute_trend_score` direction-aware**

Replace the full body of `compute_trend_score` in `scripts/trend_scan_lib/stages/ta_prefilter.py:145-181`:

```python
def compute_trend_score(indicators: dict, *, direction: str = "bullish") -> float:
    """Composite trend score. Direction determines which structural event
    (breakout vs breakdown) earns BREAKOUT_BONUS."""
    scores = {
        "ma_alignment": score_ma_alignment(
            close=indicators["close"],
            ma_20=indicators["ma_20"],
            ma_50=indicators["ma_50"],
            ma_200=indicators["ma_200"],
        ),
        "slope": score_slope(indicators.get("ma_20_series", [])),
        "rsi": score_rsi(indicators["rsi"]),
        "adx": score_adx(indicators["adx"]),
        "macd": score_macd(
            macd=indicators["macd"],
            signal=indicators["macd_signal"],
            histogram=indicators["macd_histogram"],
        ),
        "relative_strength": score_relative_strength(indicators.get("rs_vs_spy", 1.0)),
        "volume_profile": score_volume_profile(
            recent_avg_volume=indicators.get("recent_avg_volume", 0),
            avg_20d_volume=indicators.get("avg_20d_volume", 1),
            recent_up_ratio=indicators.get("recent_up_ratio", 0.5),
            up_day_volume_ratio=indicators.get("up_day_volume_ratio", 1.0),
        ),
        "bbw": score_bbw(indicators.get("bbw", 0.10)),
    }

    # NOTE: for bearish direction, ma_alignment / rsi / macd / volume_profile
    # sub-scorers currently reward bullish conditions. Invert their output so
    # a bearish-aligned ticker scores high. Simplest approach: (1 - score).
    if direction == "bearish":
        for k in ("ma_alignment", "slope", "rsi", "macd", "relative_strength", "volume_profile"):
            scores[k] = 1.0 - scores[k]

    composite = sum(scores[k] * w for k, w in INDICATOR_WEIGHTS.items())

    if direction == "bullish":
        structural = detect_breakout(
            close=indicators["close"],
            high_52w=indicators.get("high_52w", 0),
            high_20d=indicators.get("high_20d", 0),
            range_20d_pct=indicators.get("range_20d_pct", 1.0),
            atr_pct=indicators.get("atr_pct", 0),
        )
    else:
        structural = detect_breakdown(
            close=indicators["close"],
            low_52w=indicators.get("low_52w", 0),
            low_20d=indicators.get("low_20d", 0),
            range_20d_pct=indicators.get("range_20d_pct", 1.0),
            atr_pct=indicators.get("atr_pct", 0),
        )
    if structural:
        composite += BREAKOUT_BONUS

    return normalize_score(composite)
```

- [ ] **Step 6: Split Stage A and Stage BC for direction-awareness**

In `scripts/trend_scan.py`, change the pipeline topology.

**Current topology:** `_stage_a` hard-gates bullish. `_stage_bc` runs once. One candidate per ticker.
**Target topology:** `_stage_a_data` (pure fetch) runs once; `_stage_a_gate(direction)` runs per direction; `_stage_bc(direction)` runs per direction. Up to 2 candidates per ticker (one each direction).

Replace `_stage_a` with two functions:

```python
def _stage_a_data(ticker: str, data_fetcher: DataFetcher, cfg: TrendScanConfig) -> Optional[dict]:
    """Direction-neutral OHLCV fetch + liquidity/size floor. Runs once per ticker."""
    try:
        ohlcv = data_fetcher.fetch_ohlcv(ticker)
    except Exception:
        logger.warning("Stage A fetch failed for %s", ticker, exc_info=True)
        return None
    if ohlcv is None:
        return None
    if ohlcv.get("dollar_volume", 0) < cfg.min_dollar_volume:
        return None
    if ohlcv.get("market_cap", 0) < cfg.min_market_cap:
        return None
    if ohlcv.get("price", 0) < cfg.min_price:
        return None
    return ohlcv


def _stage_a_gate(ohlcv: dict, direction: str, cfg: TrendScanConfig) -> Optional[dict]:
    """Direction-specific gate + trend score. Returns ohlcv with trend_score
    attached if the direction's gate passes, else None."""
    if direction == "bullish":
        if not passes_bullish_gate(
            close=ohlcv["close"], ma_20=ohlcv["ma_20"], rsi=ohlcv["rsi"],
            dollar_volume=ohlcv["dollar_volume"],
            min_dollar_volume=cfg.min_dollar_volume,
        ):
            return None
    else:
        if not passes_bearish_gate(
            close=ohlcv["close"], ma_20=ohlcv["ma_20"], rsi=ohlcv["rsi"],
            dollar_volume=ohlcv["dollar_volume"],
            min_dollar_volume=cfg.min_dollar_volume,
        ):
            return None
    result = dict(ohlcv)
    result["trend_score"] = compute_trend_score(ohlcv, direction=direction)
    return result
```

Update `_stage_bc` signature to accept direction and pass it into `compute_structure_score`:

```python
def _stage_bc(ticker: str, ohlcv: dict, direction: str, data_fetcher: DataFetcher) -> Optional[dict]:
    try:
        struct_data = data_fetcher.fetch_structure(ticker)
        structure_score, rejected = compute_structure_score(struct_data, direction=direction)
        if rejected:
            return None
        vol_data = data_fetcher.fetch_volatility(ticker)
        vol_score, vol_flags = compute_vol_score(vol_data)
        flow_data = data_fetcher.fetch_flow(ticker)
        flow_score = compute_flow_score(flow_data, direction=direction)  # Task 9b adds direction
        return {
            "structure_score": structure_score,
            "vol_score": vol_score,
            "flow_score": flow_score,
            "vol_flags": vol_flags,
            "struct_data": struct_data,
            "vol_data": vol_data,
            "flow_data": flow_data,
        }
    except Exception:
        logger.warning("Stage B/C failed for %s / %s", ticker, direction, exc_info=True)
        return None
```

Rewrite the pipeline body in `run_scan_pipeline` (around line 530–575):

```python
    # Stage A-data — runs once per ticker (neutral fetch)
    stage_a_data_pairs = parallel_fetch(
        items=universe,
        fn=lambda ticker: (ticker, _stage_a_data(ticker, data_fetcher, cfg)),
        max_workers=cfg.max_workers,
    )
    stage_a_base = {ticker: result for ticker, result in stage_a_data_pairs if result is not None}

    # Stage A-gate + Stage BC per direction
    candidates: list[TrendCandidate] = []
    for direction in ("bullish", "bearish"):
        gated = [
            (ticker, _stage_a_gate(ohlcv, direction, cfg))
            for ticker, ohlcv in stage_a_base.items()
        ]
        gated = [(t, o) for t, o in gated if o is not None]

        bc_pairs = parallel_fetch(
            items=gated,
            fn=lambda item: (item[0], _stage_bc(item[0], item[1], direction, data_fetcher)),
            max_workers=cfg.max_workers,
        )

        for ticker, bc in bc_pairs:
            if bc is None:
                continue
            ohlcv = dict(stage_a_base[ticker])
            ohlcv["trend_score"] = gated_map := {t: o for t, o in gated}[ticker]["trend_score"]
            scores = {
                "trend": ohlcv["trend_score"],
                "structure": bc["structure_score"],
                "volatility": bc["vol_score"],
                "flow": bc["flow_score"],
            }
            candidate = TrendCandidate(
                ticker=ticker,
                direction=direction,
                final_score=compute_final_score(scores, cfg.weights),
                scores=scores,                                      # required on BaseScanCandidate
                spot_price=ohlcv.get("close", 0),
                indicators={
                    "ma_20": ohlcv.get("ma_20", 0),
                    "ma_50": ohlcv.get("ma_50", 0),
                    "ma_200": ohlcv.get("ma_200", 0),
                    "rsi": ohlcv.get("rsi", 0),
                    "adx": ohlcv.get("adx", 0),
                    "macd_histogram": ohlcv.get("macd_histogram", 0),
                    "bbw": ohlcv.get("bbw", 0),
                    "rs_vs_spy": ohlcv.get("rs_vs_spy", 0),
                    "iv_rank": bc["vol_data"].get("iv_rank", 0),
                    "gamma_flip": bc["struct_data"].get("gamma_flip", 0),
                    "call_wall": bc["struct_data"].get("call_wall", 0),
                    "put_wall": bc["struct_data"].get("put_wall", 0),
                },
                summaries={
                    "trend": _trend_summary(ohlcv, direction=direction),
                    "structure": _structure_summary(bc["struct_data"], direction=direction),
                    "vol": _vol_summary(bc["vol_data"]),
                    "flow": _flow_summary(bc["flow_data"], direction=direction),
                },
                structure_hint=_infer_structure_hint(direction, bc, ohlcv),
                invalidation=_compute_invalidation(direction, ohlcv),
                # catalysts threaded in Task 10
            )
            candidates.append(candidate)
```

Minor helper updates (add `direction` arg to `_trend_summary`, `_structure_summary`, `_flow_summary` so bearish candidates get accurate prose): one-line conditionals flipping "Above 20DMA" / "Below 20DMA", "dark-pool alignment" / "dark-pool bearish alignment", etc.

- [ ] **Step 7: Add `direction` to `compute_structure_score` — preserve `STRUCTURE_WEIGHTS` composition**

**Key correction from v1:** Do NOT rewrite `compute_structure_score` end-to-end. The existing function composes 4 sub-scores (`score_gamma_flip`, `score_net_gex`, `score_max_pain`, `score_oi_change`) via the module-level `STRUCTURE_WEIGHTS` dict. Preserve that architecture. Only the wall-support component branches on direction.

Locate the existing composition in `scripts/trend_scan_lib/stages/options_structure.py:104-125`:

```python
# Current (bullish-only):
def compute_structure_score(data: dict) -> tuple[float, bool]:
    spot = data.get("spot", 0)
    max_pain = data.get("max_pain", 0)
    gex_at_spot = data.get("gex_at_spot", 0)

    if is_severely_pinned(...):
        return 0.0, True
    if has_unsupported_overhead_wall(...):   # from Task 7
        return 0.0, True

    scores = {
        "gamma_flip": score_gamma_flip(spot=spot, gamma_flip=data.get("gamma_flip", 0)),
        "net_gex": score_net_gex(net_gex=data.get("net_gex", 0)),
        "max_pain": score_max_pain(spot=spot, max_pain=max_pain),
        "oi_change": score_oi_change(
            net_call_oi_change=data.get("net_call_oi_change", 0),
            net_put_oi_change=data.get("net_put_oi_change", 0),
        ),
    }
    composite = sum(scores[k] * w for k, w in STRUCTURE_WEIGHTS.items())
    return composite, False
```

Change to:

```python
def compute_structure_score(data: dict, *, direction: str = "bullish") -> tuple[float, bool]:
    """4-component weighted composite scored for *direction*.
    Components (preserved from v1):
      - gamma_flip: direction-aware via spot-vs-flip positioning
      - net_gex: direction-agnostic (pinning risk)
      - max_pain: direction-agnostic (magnet)
      - oi_change: direction-aware via call vs put flow signing

    Hard-fail rejects apply to both directions."""
    spot = data.get("spot", 0)
    max_pain = data.get("max_pain", 0)
    gex_at_spot = data.get("gex_at_spot", 0)

    if is_severely_pinned(spot=spot, max_pain=max_pain, gex_at_spot=gex_at_spot):
        return 0.0, True

    call_wall = data.get("call_wall", 0)
    put_wall = data.get("put_wall", 0)

    if direction == "bullish":
        if has_unsupported_overhead_wall(spot=spot, call_wall=call_wall, put_wall=put_wall):
            return 0.0, True
    else:
        if has_unsupported_underhead_wall(spot=spot, call_wall=call_wall, put_wall=put_wall):
            return 0.0, True

    scores = {
        "gamma_flip": score_gamma_flip(spot=spot, gamma_flip=data.get("gamma_flip", 0), direction=direction),
        "net_gex": score_net_gex(net_gex=data.get("net_gex", 0)),
        "max_pain": score_max_pain(spot=spot, max_pain=max_pain),
        "oi_change": score_oi_change(
            net_call_oi_change=data.get("net_call_oi_change", 0),
            net_put_oi_change=data.get("net_put_oi_change", 0),
            direction=direction,
        ),
    }
    composite = sum(scores[k] * w for k, w in STRUCTURE_WEIGHTS.items())
    return composite, False
```

Update the two sub-scorers that need direction:

```python
def score_gamma_flip(*, spot: float, gamma_flip: float, direction: str = "bullish") -> float:
    """Bullish: reward spot ABOVE flip (short-gamma = reflexive uptrend).
    Bearish: reward spot BELOW flip (short-gamma = reflexive downtrend)."""
    if gamma_flip <= 0 or spot <= 0:
        return 0.5
    raw = (spot - gamma_flip) / spot
    if direction == "bearish":
        raw = -raw
    return normalize_score(0.5 + raw * 5)


def score_oi_change(
    *,
    net_call_oi_change: float,
    net_put_oi_change: float,
    direction: str = "bullish",
) -> float:
    """Bullish: reward call OI additions + put OI removals.
    Bearish: reward put OI additions + call OI removals."""
    if direction == "bullish":
        net = net_call_oi_change - net_put_oi_change
    else:
        net = net_put_oi_change - net_call_oi_change
    return normalize_score(0.5 + net / 20_000)
```

Add the bearish mirror of the overhead-wall check (still needed for both directions):

```python
def has_unsupported_underhead_wall(*, spot: float, call_wall: float, put_wall: float) -> bool:
    """Bearish mirror: put wall within 2% below spot with no meaningful
    call resistance above means breakdown has no clean path."""
    if spot <= 0 or put_wall <= 0:
        return False
    put_underhead = (spot - put_wall) / spot
    if not (0 < put_underhead <= OVERHEAD_WALL_PCT_ABOVE):
        return False
    if call_wall > 0:
        call_resistance = (call_wall - spot) / spot
        if 0 < call_resistance <= SUPPORTIVE_PUT_PCT_BELOW:
            return False
    return True
```

Naming note (Gemini-14): `OVERHEAD_WALL_PCT_ABOVE` is reused for a "below spot" threshold here — mathematically symmetric but semantically awkward. Keep as-is for now (single threshold is the shared concept); rename to `WALL_PROXIMITY_PCT` in a follow-up cleanup if the overloading bites.

- [ ] **Step 8: Run tests**

Run: `python3.13 -m pytest scripts/tests/test_trend_scan_bearish.py scripts/tests/test_ta_prefilter.py scripts/tests/test_options_structure.py scripts/tests/test_trend_scan_e2e.py -xvs`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add scripts/trend_scan_lib/stages/ta_prefilter.py scripts/trend_scan_lib/stages/options_structure.py scripts/trend_scan.py scripts/tests/test_trend_scan_bearish.py scripts/tests/test_ta_prefilter.py scripts/tests/test_options_structure.py
git commit -m "feat(trend_scan): bearish pipeline with mirrored scoring

Spec requires bullish AND bearish candidates ranked together with
mirrored scoring. Previously direction='bullish' was hardcoded at
candidate construction AND _stage_a hard-gated on passes_bullish_gate.

Changes:
  - ta_prefilter: add passes_bearish_gate + detect_breakdown; make
    compute_trend_score direction-aware (correct BREAKOUT_BONUS path).
  - options_structure: preserve STRUCTURE_WEIGHTS 4-component composition;
    branch score_gamma_flip + score_oi_change on direction; add
    has_unsupported_underhead_wall bearish mirror of overhead check.
  - trend_scan: split _stage_a into data-fetch (neutral) + gate
    (direction-specific). _stage_bc now takes direction and threads it
    through compute_structure_score + compute_flow_score (Task 9b).
    Scanner loops both directions per ticker; up to 2 candidates emitted.

Each candidate still passes scores=scores to TrendCandidate (required
by BaseScanCandidate)."
```

---

### Task 9b: Direction-aware flow confirmation (Finding #1 continued)

**Files:**

- Modify: `scripts/trend_scan_lib/stages/flow_confirmation.py` — `compute_flow_score(data, *, direction="bullish")`.
- Modify: `scripts/trend_scan.py:_stage_bc` — `fetch_flow(ticker, direction)` (add direction arg to DataFetcher protocol).
- Modify: `scripts/trend_scan.py:LiveTrendDataFetcher.fetch_flow` — query UW with call- vs put-side filter based on direction.
- Test: add cases to `scripts/tests/test_flow_confirmation.py`.

**Context:** Task 9 left flow scoring unchanged. `fetch_flow()` currently returns call-side alerts by default; `compute_flow_score` rewards positive delta/vega and `dp_direction == "bullish"`. A bearish candidate built on that data has inverted signal. This task makes both the fetch AND the score direction-aware.

- [ ] **Step 1: Write failing test**

```python
def test_compute_flow_score_bearish_rewards_put_flow():
    """Bearish candidate: net_delta < 0 (net short delta from puts) and
    dp_direction='bearish' should score high, not penalize."""
    from scripts.trend_scan_lib.stages.flow_confirmation import compute_flow_score

    bearish_aligned = compute_flow_score({
        "ask_dominance": 0.7, "flow_count": 20, "expiry_cluster_ratio": 0.6,
        "avg_strike_pct_otm": 0.05, "net_delta": -5e6, "net_vega": 3e5,
        "dp_direction": "bearish",
    }, direction="bearish")
    bullish_same_data = compute_flow_score({
        "ask_dominance": 0.7, "flow_count": 20, "expiry_cluster_ratio": 0.6,
        "avg_strike_pct_otm": 0.05, "net_delta": -5e6, "net_vega": 3e5,
        "dp_direction": "bearish",
    }, direction="bullish")

    assert bearish_aligned > 0.6
    assert bullish_same_data < 0.4
    assert bearish_aligned > bullish_same_data
```

- [ ] **Step 2: Implement direction branch in `compute_flow_score`**

Add `direction: str = "bullish"` kwarg to `compute_flow_score`. In the existing body, negate `net_delta` and flip `dp_direction` comparison when direction == "bearish":

```python
def compute_flow_score(data: dict, *, direction: str = "bullish") -> float:
    # ... existing field reads ...
    delta = data.get("net_delta", 0)
    vega = data.get("net_vega", 0)
    dp_aligned = (
        data.get("dp_direction") == "bullish" if direction == "bullish"
        else data.get("dp_direction") == "bearish"
    )
    # When bearish, short-delta flow is the confirmation signal:
    effective_delta = delta if direction == "bullish" else -delta
    # ... continue existing scoring with effective_delta and dp_aligned ...
```

- [ ] **Step 3: Make `LiveTrendDataFetcher.fetch_flow` direction-aware**

Update the UW query to filter by side based on direction. Default bullish for backward compatibility.

- [ ] **Step 4: Run tests**

Run: `python3.13 -m pytest scripts/tests/test_flow_confirmation.py scripts/tests/test_trend_scan_bearish.py -xvs`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/trend_scan_lib/stages/flow_confirmation.py scripts/trend_scan.py scripts/tests/test_flow_confirmation.py
git commit -m "feat(trend_scan): direction-aware flow confirmation

compute_flow_score() now inverts net_delta and dp_direction comparison
for bearish candidates, so short-delta + bearish-dark-pool flow is
treated as CONFIRMATION, not rejection. Default remains bullish for
backward compatibility with single-direction test fixtures."
```

---

### Task 10: Stage C catalyst check (Finding #4)

**Files:**

- Create: `scripts/trend_scan_lib/stages/catalysts.py`
- Modify: `scripts/trend_scan_lib/stages/flow_confirmation.py` (wire catalyst into flow score)
- Modify: `scripts/trend_scan.py` (pass UW client to flow stage, thread catalysts)
- Test: new `scripts/tests/test_trend_scan_catalysts.py`

- [ ] **Step 1: Design the catalyst interface**

A catalyst is a typed tag: `earnings_within_7d`, `fda_pdufa`, `analyst_upgrade`, `analyst_downgrade`, `guidance_raise`, `guidance_cut`, `activist`, `ma_rumor`, `headline_momentum`. The stage returns `(catalysts: list[str], score: float)` where score ∈ [0,1] and represents catalyst favorability for the direction.

- [ ] **Step 2: Write failing test**

Create `scripts/tests/test_trend_scan_catalysts.py`:

```python
"""Stage C catalyst stage tests."""
from unittest.mock import MagicMock

def test_catalyst_stage_degrades_gracefully_without_uw_client():
    """If UW client is None (unavailable / budget exhausted), stage returns
    empty catalysts and neutral score — never raises."""
    from scripts.trend_scan_lib.stages.catalysts import fetch_catalysts

    catalysts, score = fetch_catalysts(
        ticker="AAPL", direction="bullish", uw_client=None,
        earnings_days=30,
    )
    assert catalysts == []
    assert score == 0.5


def test_catalyst_stage_flags_imminent_earnings():
    """Earnings within 7 days is always a catalyst (direction-agnostic —
    creates event risk either way). Scored as neutral (0.5) since we
    don't predict direction of the move."""
    from scripts.trend_scan_lib.stages.catalysts import fetch_catalysts

    catalysts, score = fetch_catalysts(
        ticker="AAPL", direction="bullish", uw_client=None,
        earnings_days=3,
    )
    assert "earnings_within_7d" in catalysts
    assert score == 0.5  # neutral — event risk isn't directional


def test_catalyst_stage_rewards_bullish_aligned_headlines():
    """UW headline fetch returning upgrade/guidance-raise headlines
    for a bullish candidate should score > 0.6."""
    from scripts.trend_scan_lib.stages.catalysts import fetch_catalysts

    fake_uw = MagicMock()
    fake_uw.get_headlines.return_value = [
        {"type": "analyst_upgrade", "ts": "2026-04-14T09:00:00Z"},
        {"type": "guidance_raise", "ts": "2026-04-14T08:00:00Z"},
    ]

    catalysts, score = fetch_catalysts(
        ticker="AAPL", direction="bullish", uw_client=fake_uw,
        earnings_days=30,
    )
    assert "analyst_upgrade" in catalysts
    assert "guidance_raise" in catalysts
    assert score > 0.6


def test_catalyst_stage_penalizes_bullish_against_bearish_headlines():
    """A bullish candidate with analyst_downgrade headlines scores low."""
    from scripts.trend_scan_lib.stages.catalysts import fetch_catalysts

    fake_uw = MagicMock()
    fake_uw.get_headlines.return_value = [
        {"type": "analyst_downgrade", "ts": "2026-04-14T09:00:00Z"},
    ]

    catalysts, score = fetch_catalysts(
        ticker="AAPL", direction="bullish", uw_client=fake_uw,
        earnings_days=30,
    )
    assert "analyst_downgrade" in catalysts
    assert score < 0.4
```

- [ ] **Step 3: Run tests to verify failure**

Run: `python3.13 -m pytest scripts/tests/test_trend_scan_catalysts.py -xvs`
Expected: FAIL — module does not exist.

- [ ] **Step 4: Implement `scripts/trend_scan_lib/stages/catalysts.py`**

```python
"""Stage C catalyst detection — headlines + event flags.

Always degrades gracefully. If UW client is None or raises, returns
(empty list, 0.5) — catalyst information is informational, not
gate-forming."""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_BULLISH_TYPES = {
    "analyst_upgrade", "guidance_raise", "ma_rumor_acquirer_of",
    "activist_long", "fda_pdufa_positive",
}
_BEARISH_TYPES = {
    "analyst_downgrade", "guidance_cut", "activist_short",
    "fraud_allegation", "fda_pdufa_negative",
}
_NEUTRAL_TYPES = {"earnings_within_7d", "headline_momentum", "ma_rumor_target_of"}


def fetch_catalysts(
    *,
    ticker: str,
    direction: str,
    uw_client: Optional[object],
    earnings_days: int,
) -> tuple[list[str], float]:
    """Return (catalyst tags, score in [0,1]) for *ticker* in *direction*.

    Score convention:
      > 0.6  — catalysts align with direction
      0.5    — neutral (no catalysts, or event risk only)
      < 0.4  — catalysts oppose direction
    """
    catalysts: list[str] = []

    # Event risk from earnings
    if 0 <= earnings_days <= 7:
        catalysts.append("earnings_within_7d")

    # UW-sourced headlines — best effort
    if uw_client is not None:
        try:
            headlines = uw_client.get_headlines(ticker) or []
        except Exception as exc:
            logger.warning("fetch_catalysts: UW headlines failed for %s: %s", ticker, exc)
            headlines = []

        for h in headlines:
            t = h.get("type")
            if t and t not in catalysts and (t in _BULLISH_TYPES or t in _BEARISH_TYPES or t in _NEUTRAL_TYPES):
                catalysts.append(t)

    # Score alignment
    aligned_set = _BULLISH_TYPES if direction == "bullish" else _BEARISH_TYPES
    opposed_set = _BEARISH_TYPES if direction == "bullish" else _BULLISH_TYPES
    aligned = sum(1 for c in catalysts if c in aligned_set)
    opposed = sum(1 for c in catalysts if c in opposed_set)

    if aligned == 0 and opposed == 0:
        return catalysts, 0.5
    # Map aligned-vs-opposed into [0,1]
    raw = (aligned - opposed) / max(aligned + opposed, 1)  # [-1, 1]
    score = 0.5 + raw / 2  # [0, 1]
    return catalysts, score
```

- [ ] **Step 5: Wire catalyst into scanner loop**

Add import at top of `scripts/trend_scan.py`:

```python
from scripts.trend_scan_lib.stages.catalysts import fetch_catalysts
```

`run_scan_pipeline` is a module-level function (no `self`). The `uw_client` parameter is already in its signature (`run_scan_pipeline(cfg, *, data_fetcher, uw_client, ib_client, ...)`), so thread it into the loop directly:

Inside the Task 9 Step 6 pipeline body, add BEFORE the `TrendCandidate(...)` construction:

```python
            catalysts, catalyst_score = fetch_catalysts(
                ticker=ticker,
                direction=direction,
                uw_client=uw_client,                                  # from run_scan_pipeline args
                earnings_days=bc["vol_data"].get("earnings_days", 30),  # NOT ohlcv — tribunal fix
            )
            scores["catalyst"] = catalyst_score
```

And pass `catalysts=catalysts` into the `TrendCandidate(...)` call.

**Tribunal fix:** `earnings_days` lives in `vol_data` (produced by `fetch_volatility()`), not in `ohlcv`. v1 plan used `ohlcv.get("earnings_days", 30)` which would silently default to 30 for every candidate.

- [ ] **Step 6: Update config weights — EXPLICIT dict**

Replace the `weights` default in `scripts/trend_scan_lib/config.py:20-26`:

```python
    # Scoring weights — must sum to 1.0 exactly (enforced by weighted_composite)
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "trend":      0.30,   # was 0.35
            "structure":  0.25,   # unchanged
            "volatility": 0.20,   # unchanged
            "flow":       0.15,   # was 0.20
            "catalyst":   0.10,   # NEW
        }
    )
```

Sum check: 0.30 + 0.25 + 0.20 + 0.15 + 0.10 = 1.00 ✓.

Update `scripts/tests/test_trend_config.py` default-weights assertion to the new dict. Grep for any other code asserting weight values: `grep -rn '"trend":\s*0\.35\|"flow":\s*0\.20' scripts/`.

- [ ] **Step 7: Run full suite**

Run: `python3.13 -m pytest scripts/tests/ -xvs -k "trend or catalyst or ta_"`
Expected: PASS.

- [ ] **Step 8: Smoke-run end to end**

Run: `python3.13 scripts/trend_scan.py --top 10`
Expected: exits 0; output candidates now include `catalysts: [...]` and `scores.catalyst`. If UW is unavailable, catalysts are empty and scan still completes.

- [ ] **Step 9: Commit**

```bash
git add scripts/trend_scan_lib/stages/catalysts.py scripts/trend_scan_lib/config.py scripts/trend_scan.py scripts/tests/test_trend_scan_catalysts.py
git commit -m "feat(trend_scan): Stage C catalyst check via UW headlines

Spec requires headlines + earnings/FDA/catalyst flags in flow
confirmation. Prior implementation only checked earnings_days
for vol scoring — no headline fetching, no FDA/activist/guidance
tagging.

New stages/catalysts.py fetches headlines from UW client (best
effort, degrades to neutral score if unavailable), tags candidates
with typed catalyst labels, and contributes a directional score
component weighted 0.1 in final ranking. Catalyst list surfaces
in candidate.to_dict() for downstream review."
```

---

# Phase 3 Checkpoint

- [ ] Full regression:
      `python3.13 scripts/run_pytest_affected.py`
      Expected: all green.
- [ ] Manual scan:
      `python3.13 scripts/trend_scan.py --top 25`
      Expected: candidates include bullish AND bearish; each has `structure_hint`, `catalysts`, `flags: ["four_gates_not_applied"]`.
- [ ] Verify JSON output:
      `cat data/trend_scan.json | jq '.candidates[0]'`
      Expected: all new fields present; no `suggested_trade` field anywhere.
- [ ] Review all Phase 3 commits and request codex-review before pushing branch.

---

# Completion Criteria

- [ ] All 9 tribunal findings addressed (1–9).
- [ ] All 12 code-review tribunal (v2) findings addressed.
- [ ] `scripts/tests/test_ta_lib/` + all `test_trend_*.py` + `test_options_structure.py` + `test_flow_confirmation.py` + `test_trend_storage.py` passing.
- [ ] `cd web && npm test` passing (TS types aligned with new schema).
- [ ] `python3.13 scripts/trend_scan.py --top 10` completes successfully against live TA cache with **both bullish and bearish** candidates in output.
- [ ] `data/trend_scan.json` candidates have `direction ∈ {bullish, bearish}`, no `suggested_trade` field, present `structure_hint` + `catalysts` + `scores.catalyst` + `flags` containing `"four_gates_not_applied"`.
- [ ] `data/ta_premarket_universe.json` persists after `ta_premarket_prep.py` runs; scanner consumes it if <2h old.
- [ ] `data/trend_scan.duckdb` schema no longer has `suggested_trade` column; has `structure_hint` + `catalysts` (JSON string).
- [ ] SPY outage smoke-test: temporarily rename SPY's DuckDB rows, re-run scan, confirm no crash and candidates still emitted with warning logged.
- [ ] `--audit-only` offline smoke: `UW_TOKEN= python3.13 scripts/ta_premarket_prep.py --audit-only` exits 0 with no network calls.

---

# Out of Scope (Explicitly Dismissed by Tribunal)

- atr_pct persistence (works fine recomputed each snapshot).
- Ask-dominance put-flow edge case (narrow impact).
- Universe sources parallelization (perf only).

Do not expand into these during this plan's execution.
