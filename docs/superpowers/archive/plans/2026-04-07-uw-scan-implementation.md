# uw-scan + uw-analyze (Feature B primary, Feature A library) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the `uw-scan` tiered opportunity scanner (4 signals: Deep Conviction Flow, GEX Pinning, Earnings IV Crush, Dark Pool Accumulation) plus a shared per-ticker analysis library (VRP state, regime, 4-bucket composite) consumed via `uw_analyze.run_analysis()`, with a thin `uw-analyze` CLI for deep-dive lookups. Analysis-only — no trade emission.

**Scope note (v1):** `uw-scan` supports **watchlist** and **targeted** modes only. Market-wide mode is deferred to a follow-up spec — it requires a universe loader on top of `fetch_options_flow` aggregation, which is already covered by the existing `discover.py` command with a different signal model. Shipping watchlist + targeted first gives the highest-leverage user journey without duplicating `discover`'s flow-led universe logic.

**Architecture:** Pure-function analysis library in `scripts/analysis/` feeds two thin CLIs (`scripts/uw_scan.py`, `scripts/uw_analyze.py`). Scanner uses existing `UWClient` REST wrappers, ThreadPoolExecutor parallelism matching `discover.py`. Missing-data policy is silent degradation with a `data_freshness` map per report. No naked-short guard needed — zero trade ideas are emitted.

**Tech Stack:** Python 3.13/3.14, pytest, existing `scripts/clients/uw_client.py`, `concurrent.futures.ThreadPoolExecutor`, frozen `@dataclass` for all state objects.

**Design specs:**
- `docs/superpowers/specs/2026-04-07-uw-scan-opportunity-design.md` (primary)
- `docs/superpowers/specs/2026-04-07-uw-analyze-ticker-design.md` (shared library)

---

## File structure and responsibilities

**New files (created by this plan):**

```
scripts/
  analysis/
    __init__.py                       # package marker, empty
    models.py                         # VRPState, RegimeState, BucketScores, BenchmarkContext,
                                      #   BenchmarkSnapshot, TickerData, AnalysisReport (all @dataclass(frozen=True))
    ticker_data.py                    # fetch_ticker_data(ticker, client) → TickerData
                                      #   iv_rank normalization rule enforced here
    vrp.py                            # build_vrp_state(td) → VRPState; classify_regime(td, vrp) → RegimeState
    gex.py                            # detect_flip_point, rank_walls, detect_pinning
    benchmark.py                      # load_benchmark_context(client) → BenchmarkContext
    scoring.py                        # score_buckets(td, vrp, regime, mode) → BucketScores
    gates.py                          # earnings_gate, liquidity_gate, regime_gate (pure helpers)
  uw_scan_lib/
    __init__.py
    models.py                         # SignalHit, ContextFlag, ScanCandidate, ScanResult
    universe.py                       # load_universe(mode, watchlist, tickers) → list[str]
    signals/
      __init__.py
      deep_conviction_flow.py         # detect(ticker, td) → SignalHit | None
      gex_pinning.py
      earnings_iv_crush.py
      dark_pool_accumulation.py
    context/
      __init__.py
      pcr_sentiment.py                # flag(ticker, td) → ContextFlag | None (zero weight)
    confluence.py                     # detect_type_f(hits) → bool per ticker
    ranking.py                        # rank_candidates(candidates) → sorted list[ScanCandidate]
  uw_analyze.py                       # run_analysis(ticker, *, fast, client) + argparse CLI
  uw_scan.py                          # scan_universe(...) + argparse CLI

scripts/tests/
  test_analysis_models.py
  test_analysis_ticker_data.py
  test_analysis_vrp.py
  test_analysis_gex.py
  test_analysis_benchmark.py
  test_analysis_scoring.py
  test_analysis_gates.py
  test_uw_analyze.py
  test_uw_scan_models.py
  test_uw_scan_universe.py
  test_uw_scan_signals_deep_conviction.py
  test_uw_scan_signals_gex_pinning.py
  test_uw_scan_signals_earnings_iv_crush.py
  test_uw_scan_signals_dark_pool.py
  test_uw_scan_context_pcr.py
  test_uw_scan_confluence.py
  test_uw_scan_ranking.py
  test_uw_scan.py

data/
  analysis/                           # gitignored; uw-analyze output goes here
    .gitkeep
  uw_scan/                            # gitignored; uw-scan output goes here
    .gitkeep
```

**Modified files:**

- `scripts/CLAUDE.md` — add `uw-scan` and `uw-analyze` rows to the commands table; document `uw-scan` vs existing `scan` positioning
- `scripts/clients/uw_client.py` — **ONLY IF** endpoint probe (Task 0) returns 200: add `get_variance_risk_premium(ticker, timespan="1y")` wrapper
- `.gitignore` — add `data/analysis/` and `data/uw_scan/` entries (append to existing data/ patterns)

**NOT touched:** `scripts/evaluate.py`, `scripts/discover.py`, `scripts/scanner.py`, `scripts/leap_scanner_uw.py`, `web/`, `scripts/api/`.

**Build order (TDD, bottom-up):**
1. **Phase 0:** endpoint probe (Task 0)
2. **Phase 1 — shared analysis library:** models → ticker_data → vrp → gex → benchmark → scoring → gates (Tasks 1–7)
3. **Phase 2 — uw_analyze:** `run_analysis()` + CLI (Task 8)
4. **Phase 3 — uw_scan_lib signals:** models → universe → 4 signals → pcr → confluence → ranking (Tasks 9–15)
5. **Phase 4 — uw_scan:** orchestration + CLI + chaining (Task 16)
6. **Phase 5 — docs + gitignore** (Task 17)

Each task ships tests + implementation + commit in that order. No placeholders, no skipped commits.

---

## Pre-flight checks

Before starting Task 0, the implementer must:

- [ ] **Verify branch.** Run `git status` — clean working tree, on a feature branch (not master).
- [ ] **Verify Python.** Run `python3 --version` — expect 3.13 or 3.14.
- [ ] **Verify pytest.** Run `pytest --version` — expect a version present.
- [ ] **Verify UW token.** Run `python3 -c "import os; assert os.environ.get('UW_TOKEN'), 'UW_TOKEN not set'"` — exit 0.
- [ ] **Verify existing UW client imports work.** Run `python3 -c "from scripts.clients.uw_client import UWClient; print('ok')"` from repo root — expect `ok`.

If any check fails, STOP and fix before proceeding.

---

## Task 0: Probe the VRP endpoint

**Files:**
- Touches no code. This is a runtime probe that gates the optional `get_variance_risk_premium` wrapper.

**Why:** The spec's #1 risk is that `/api/volatility/variance_risk_premium/{T}?timespan=1y` may not exist on Xenon's UW plan. Decide NOW so later tasks know whether to use the real endpoint or ship with `vrp_zscore = None` from the start.

- [ ] **Step 1: Probe the endpoint directly with curl**

```bash
curl -sS -w "\nHTTP=%{http_code}\n" \
  -H "Authorization: Bearer $UW_TOKEN" \
  -H "Accept: application/json" \
  "https://api.unusualwhales.com/api/volatility/variance_risk_premium/SPY?timespan=1y" \
  | tail -20
```

Expected: one of
- `HTTP=200` with a JSON body containing a time series → **enable** VRP wrapper path
- `HTTP=404` or `HTTP=403` or `HTTP=401` → **skip** VRP wrapper path

- [ ] **Step 2: Record the decision**

Write one line to `docs/superpowers/plans/notes/2026-04-07-vrp-endpoint-probe.md`:
- If 200: `VRP endpoint: AVAILABLE — add get_variance_risk_premium wrapper in Task 3`
- If non-200: `VRP endpoint: UNAVAILABLE (HTTP <code>) — skip wrapper; vrp_zscore is always None in this ship`

This file drives the implementer's choice in Task 3 and Task 5.

- [ ] **Step 3: Commit the probe result**

```bash
mkdir -p docs/superpowers/plans/notes
# (create the note file above)
git add docs/superpowers/plans/notes/2026-04-07-vrp-endpoint-probe.md
git commit -m "chore(uw-scan): record VRP endpoint probe result"
```

---

## Task 1: `scripts/analysis/` package skeleton + models

**Files:**
- Create: `scripts/analysis/__init__.py`
- Create: `scripts/analysis/models.py`
- Test: `scripts/tests/test_analysis_models.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_analysis_models.py
from datetime import datetime
from scripts.analysis.models import (
    VRPState, RegimeState, BucketScores, BenchmarkSnapshot, BenchmarkContext,
    TickerData, AnalysisReport,
)


def test_vrp_state_allows_optional_fields():
    s = VRPState(
        vrp_raw=None, vrp_zscore=None, iv_percentile=None,
        ts_ratio=None, ts_inverted=None, earnings_within_14d=True,
        data_freshness="unavailable",
    )
    assert s.earnings_within_14d is True
    assert s.data_freshness == "unavailable"


def test_bucket_scores_requires_mode_and_bias():
    b = BucketScores(
        market_structure=10.0, volatility=-5.0, flow=0.0, positioning=0.0,
        composite=5.0, grade="B", bias="BULLISH",
        mode="full", reweighted=False, skipped_buckets=[],
    )
    assert b.mode == "full"
    assert b.bias == "BULLISH"
    assert b.grade == "B"


def test_regime_state_only_allows_r0_r1_r2():
    r = RegimeState(
        regime="R0", reason="test",
        gex_sign="positive", gex_flip_relative="below_price", flip_distance_pct=1.5,
    )
    assert r.regime == "R0"


def test_ticker_data_bucket_available_rules():
    td_full = TickerData(
        ticker="TSLA", price=200.0, fetched_at=datetime.now(),
        gex={"net": 1.0}, gex_by_strike={"strikes": []},
        iv=30.0, rv=25.0, iv_percentile=60.0, term_structure=[{"iv": 0.3}],
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=[{}], net_premium=None, pcr=None, darkpool=None,
        oi_changes=[{}], short_interest=None,
        earnings_date=None, earnings_within_14d=True,
    )
    assert td_full.bucket_available("market_structure") is True
    assert td_full.bucket_available("volatility") is True
    assert td_full.bucket_available("flow") is True
    # v1 LIMITATION: positioning is always unavailable (OI history deferred).
    assert td_full.bucket_available("positioning") is False

    td_empty = TickerData(
        ticker="X", price=None, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=None, rv=None, iv_percentile=None, term_structure=None,
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=None, net_premium=None, pcr=None, darkpool=None,
        oi_changes=None, short_interest=None,
        earnings_date=None, earnings_within_14d=True,
    )
    assert td_empty.bucket_available("market_structure") is False
    assert td_empty.bucket_available("volatility") is False
    assert td_empty.bucket_available("flow") is False
    assert td_empty.bucket_available("positioning") is False


def test_analysis_report_roundtrip():
    r = AnalysisReport(
        ticker="AAPL", price=180.0, fetched_at="2026-04-07T10:00:00",
        data_freshness={"gex": "live"},
        benchmark=BenchmarkContext(spy=BenchmarkSnapshot(
            ticker="SPY", iv_rank=35.0, gex_regime="positive", gex_flip=450.0,
            price=460.0, data_date="2026-04-07", freshness="live",
        ), sector_etf=None),
        vrp=VRPState(
            vrp_raw=5.0, vrp_zscore=0.8, iv_percentile=55.0,
            ts_ratio=0.95, ts_inverted=False,
            earnings_within_14d=False, data_freshness="live",
        ),
        regime=RegimeState(
            regime="R0", reason="ok", gex_sign="positive",
            gex_flip_relative="below_price", flip_distance_pct=2.0,
        ),
        scores=BucketScores(
            market_structure=15.0, volatility=10.0, flow=12.0, positioning=8.0,
            composite=45.0, grade="A", bias="BULLISH",
            mode="full", reweighted=False, skipped_buckets=[],
        ),
        notes=["test note"],
    )
    assert r.ticker == "AAPL"
    assert r.scores.composite == 45.0
```

- [ ] **Step 2: Run the test, verify it fails**

```bash
pytest scripts/tests/test_analysis_models.py -v
```
Expected: `ModuleNotFoundError: No module named 'scripts.analysis'`

- [ ] **Step 3: Create `scripts/analysis/__init__.py`**

```python
# scripts/analysis/__init__.py
"""Shared per-ticker analysis library for uw-scan and uw-analyze."""
```

- [ ] **Step 4: Create `scripts/analysis/models.py`**

```python
# scripts/analysis/models.py
"""Frozen dataclass types for the analysis library.

All fields that can legitimately be None due to missing-data degradation
are typed Optional. See the Missing-data policy in the design doc.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Optional


@dataclass(frozen=True)
class VRPState:
    vrp_raw: Optional[float]
    vrp_zscore: Optional[float]
    iv_percentile: Optional[float]
    ts_ratio: Optional[float]
    ts_inverted: Optional[bool]
    earnings_within_14d: bool
    data_freshness: Literal["live", "stale", "unavailable"]


@dataclass(frozen=True)
class RegimeState:
    regime: Literal["R0", "R1", "R2"]
    reason: str
    gex_sign: Optional[Literal["positive", "negative", "mixed"]]
    gex_flip_relative: Optional[Literal["above_price", "below_price", "at_price"]]
    flip_distance_pct: Optional[float]


@dataclass(frozen=True)
class BucketScores:
    market_structure: float
    volatility: float
    flow: float
    positioning: float
    composite: float
    grade: Literal["A", "B", "C"]
    bias: Literal[
        "STRONGLY_BULLISH", "BULLISH", "MIXED", "BEARISH", "STRONGLY_BEARISH"
    ]
    mode: Literal["full", "fast"]
    reweighted: bool
    skipped_buckets: list[str]


@dataclass(frozen=True)
class BenchmarkSnapshot:
    ticker: str
    iv_rank: Optional[float]
    gex_regime: Optional[Literal["positive", "negative", "mixed"]]
    gex_flip: Optional[float]
    price: Optional[float]
    data_date: Optional[str]
    freshness: Literal["live", "stale", "unavailable"]


@dataclass(frozen=True)
class BenchmarkContext:
    spy: BenchmarkSnapshot
    sector_etf: Optional[BenchmarkSnapshot]


@dataclass(frozen=True)
class TickerData:
    ticker: str
    price: Optional[float]
    fetched_at: datetime
    # Market Structure bucket
    gex: Optional[dict]
    gex_by_strike: Optional[dict]
    # Volatility bucket
    iv: Optional[float]
    rv: Optional[float]
    iv_percentile: Optional[float]
    term_structure: Optional[list[dict]]
    rr_skew_25d: Optional[float]
    vrp_history: Optional[list[float]]
    # Flow bucket
    flow_alerts: Optional[list[dict]]
    net_premium: Optional[dict]
    pcr: Optional[float]
    darkpool: Optional[dict]
    # Positioning bucket
    oi_changes: Optional[list[dict]]
    short_interest: Optional[dict]
    # Context
    earnings_date: Optional[date]
    earnings_within_14d: bool

    def bucket_available(
        self,
        bucket: Literal[
            "market_structure", "volatility", "flow", "positioning"
        ],
    ) -> bool:
        if bucket == "market_structure":
            return self.gex is not None and self.gex_by_strike is not None
        if bucket == "volatility":
            return (
                self.iv is not None
                and self.iv_percentile is not None
                and self.term_structure is not None
            )
        if bucket == "flow":
            return self.flow_alerts is not None or self.net_premium is not None
        if bucket == "positioning":
            # v1 LIMITATION: positioning always unavailable — OI history and
            # short interest scoring are deferred. Bucket is always reweighted out.
            return False
        raise ValueError(f"unknown bucket: {bucket}")


@dataclass(frozen=True)
class AnalysisReport:
    ticker: str
    price: Optional[float]
    fetched_at: str
    data_freshness: dict[str, str]
    benchmark: BenchmarkContext
    vrp: VRPState
    regime: RegimeState
    scores: BucketScores
    notes: list[str]
```

- [ ] **Step 5: Run the test, verify it passes**

```bash
pytest scripts/tests/test_analysis_models.py -v
```
Expected: all 5 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add scripts/analysis/__init__.py scripts/analysis/models.py scripts/tests/test_analysis_models.py
git commit -m "feat(analysis): add frozen dataclass models for analysis library"
```

---

## Task 2: `TickerData` fetcher with `iv_rank` normalization

**Files:**
- Create: `scripts/analysis/ticker_data.py`
- Test: `scripts/tests/test_analysis_ticker_data.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_analysis_ticker_data.py
from datetime import datetime
from unittest.mock import MagicMock

from scripts.analysis.ticker_data import fetch_ticker_data


def _make_mock_client(*, vol_stats=None, gex=None, gex_by_strike=None,
                      term_structure=None, flow_alerts=None, oi_changes=None,
                      darkpool=None, earnings=None, short_data=None):
    c = MagicMock()
    c.get_volatility_stats.return_value = vol_stats or {}
    c.get_greek_exposure.return_value = gex or {}
    c.get_greek_exposure_by_strike.return_value = gex_by_strike or {}
    c.get_volatility_term_structure.return_value = term_structure or {}
    c.get_flow_alerts.return_value = {"data": flow_alerts or []}
    c.get_darkpool_flow.return_value = darkpool or {}
    c.get_earnings_by_ticker.return_value = earnings or {"data": []}
    c.get_short_data.return_value = short_data or {}
    return c


def test_iv_rank_normalization_raw_float_to_percentile():
    """Raw UW iv_rank=0.65 must become iv_percentile=65.0 (0-100 scale)."""
    c = _make_mock_client(vol_stats={"iv": "0.28", "rv": "0.21", "iv_rank": "0.65"})
    td = fetch_ticker_data("TSLA", c)
    assert td.iv_percentile == 65.0
    assert td.iv == 28.0   # also scaled to 0-100
    assert td.rv == 21.0


def test_missing_vol_stats_leaves_fields_none():
    c = _make_mock_client(vol_stats={})
    td = fetch_ticker_data("TSLA", c)
    assert td.iv is None
    assert td.rv is None
    assert td.iv_percentile is None


def test_earnings_within_14d_true_when_unknown():
    c = _make_mock_client(earnings={"data": []})
    td = fetch_ticker_data("TSLA", c)
    assert td.earnings_within_14d is True  # conservative default


def test_ticker_data_ticker_is_uppercased():
    c = _make_mock_client()
    td = fetch_ticker_data("tsla", c)
    assert td.ticker == "TSLA"


def test_ticker_data_fetched_at_is_datetime():
    c = _make_mock_client()
    td = fetch_ticker_data("TSLA", c)
    assert isinstance(td.fetched_at, datetime)


def test_vrp_history_populated_when_wrapper_available():
    c = _make_mock_client()
    c.get_variance_risk_premium = MagicMock(return_value={
        "data": [{"vrp": 0.1}, {"vrp": 0.2}, {"vrp": 0.3}],
    })
    td = fetch_ticker_data("TSLA", c)
    assert td.vrp_history == [0.1, 0.2, 0.3]


def test_vrp_history_none_when_wrapper_missing():
    c = _make_mock_client()
    # No get_variance_risk_premium attribute at all
    if hasattr(c, "get_variance_risk_premium"):
        del c.get_variance_risk_premium
    td = fetch_ticker_data("TSLA", c)
    assert td.vrp_history is None


def test_pcr_derived_from_flow_alerts_call_put_counts():
    c = _make_mock_client(flow_alerts=[
        {"option_type": "call"}, {"option_type": "call"}, {"option_type": "put"},
    ])
    td = fetch_ticker_data("TSLA", c)
    assert td.pcr == 0.5  # 1 put / 2 calls


def test_pcr_none_when_no_calls():
    c = _make_mock_client(flow_alerts=[{"option_type": "put"}])
    td = fetch_ticker_data("TSLA", c)
    assert td.pcr is None
```

- [ ] **Step 2: Run the test, verify it fails**

```bash
pytest scripts/tests/test_analysis_ticker_data.py -v
```
Expected: `ModuleNotFoundError: No module named 'scripts.analysis.ticker_data'`

- [ ] **Step 3: Create `scripts/analysis/ticker_data.py`**

```python
# scripts/analysis/ticker_data.py
"""TickerData aggregator.

Fetches all per-ticker data a scan/analyze run needs, with one single
normalization step for iv_rank (raw 0..1 float → 0..100 percentile).

All downstream code uses TickerData.iv_percentile and never touches raw iv_rank.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from scripts.analysis.models import TickerData

logger = logging.getLogger(__name__)


def _to_float_times_100(v) -> Optional[float]:
    """Parse a raw UW fraction (string or float, 0..1 scale) to 0..100."""
    if v is None or v == "":
        return None
    try:
        return float(v) * 100.0
    except (TypeError, ValueError):
        return None


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_next_earnings(raw: dict) -> tuple[Optional[date], bool]:
    """From UW get_earnings_by_ticker response, derive the next earnings date.

    UW returns a list of earnings events (historical + upcoming). We treat any
    event with a date >= today as "upcoming" and take the soonest.
    Returns (date_or_None, within_14d_bool). If unknown, conservative True.
    """
    events = raw.get("data") or []
    if not isinstance(events, list) or not events:
        return None, True  # conservative

    today = datetime.now().date()
    upcoming: list[date] = []
    for ev in events:
        raw_date = ev.get("report_date") or ev.get("date")
        if not raw_date:
            continue
        try:
            d = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if d >= today:
            upcoming.append(d)

    if not upcoming:
        return None, True  # nothing upcoming — default conservative

    next_date = min(upcoming)
    return next_date, (next_date - today) <= timedelta(days=14)


def fetch_ticker_data(ticker: str, client) -> TickerData:
    """Fetch all per-ticker inputs and return a TickerData.

    Silent degradation: any individual endpoint failure leaves that field None.
    """
    ticker = ticker.upper()
    fetched_at = datetime.now()

    # Volatility stats (drives iv_percentile normalization)
    iv = rv = iv_percentile = None
    try:
        vol = client.get_volatility_stats(ticker) or {}
        iv = _to_float_times_100(vol.get("iv"))
        rv = _to_float_times_100(vol.get("rv"))
        iv_percentile = _to_float_times_100(vol.get("iv_rank"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("volatility_stats failed for %s: %s", ticker, exc)

    # Term structure
    term_structure = None
    try:
        ts = client.get_volatility_term_structure(ticker) or {}
        ts_data = ts.get("data") if isinstance(ts, dict) else None
        if isinstance(ts_data, list) and ts_data:
            term_structure = ts_data
    except Exception as exc:  # noqa: BLE001
        logger.debug("term_structure failed for %s: %s", ticker, exc)

    # GEX
    gex = gex_by_strike = None
    price = None
    try:
        gex_resp = client.get_greek_exposure(ticker) or {}
        gex = gex_resp if gex_resp else None
        # Price often appears in GEX response
        if isinstance(gex_resp, dict):
            price = _to_float(gex_resp.get("price") or gex_resp.get("spot"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("greek_exposure failed for %s: %s", ticker, exc)

    try:
        gbs = client.get_greek_exposure_by_strike(ticker) or {}
        gex_by_strike = gbs if gbs else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("greek_exposure_by_strike failed for %s: %s", ticker, exc)

    # Flow alerts
    flow_alerts = None
    try:
        fa = client.get_flow_alerts(ticker=ticker, limit=50) or {}
        data = fa.get("data") if isinstance(fa, dict) else None
        flow_alerts = data if isinstance(data, list) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("flow_alerts failed for %s: %s", ticker, exc)

    # Dark pool — trailing 5 calendar days, concatenate into a single {"data": [...]}.
    darkpool = None
    try:
        all_trades: list[dict] = []
        for days_ago in range(5):
            date_str = (datetime.now().date() - timedelta(days=days_ago)).isoformat()
            try:
                dp = client.get_darkpool_flow(ticker, date=date_str) or {}
            except Exception as exc:  # noqa: BLE001
                logger.debug("darkpool_flow %s %s failed: %s", ticker, date_str, exc)
                continue
            rows = dp.get("data") if isinstance(dp, dict) else None
            if isinstance(rows, list):
                all_trades.extend(rows)
        if all_trades:
            darkpool = {"data": all_trades}
    except Exception as exc:  # noqa: BLE001
        logger.debug("darkpool window failed for %s: %s", ticker, exc)

    # Earnings
    earnings_date, earnings_within_14d = None, True
    try:
        er = client.get_earnings_by_ticker(ticker) or {}
        earnings_date, earnings_within_14d = _parse_next_earnings(er)
    except Exception as exc:  # noqa: BLE001
        logger.debug("earnings_by_ticker failed for %s: %s", ticker, exc)

    # Short data — optional
    short_interest = None
    try:
        sd = client.get_short_data(ticker) or {}
        short_interest = sd if sd else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("short_data failed for %s: %s", ticker, exc)

    # Historical skew — informational only for v1
    rr_skew_25d = None
    try:
        rr = client.get_historical_risk_reversal_skew(ticker) or {}
        # Extract the most recent 25Δ value; shape varies, be defensive
        items = rr.get("data") if isinstance(rr, dict) else None
        if isinstance(items, list) and items:
            latest = items[-1]
            if isinstance(latest, dict):
                rr_skew_25d = _to_float(latest.get("skew_25") or latest.get("value"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("rr_skew failed for %s: %s", ticker, exc)

    # VRP history — conditional on endpoint availability (Task 0 probe result).
    # If the probe recorded AVAILABLE, the wrapper exists on UWClient.
    vrp_history = None
    fetch_vrp = getattr(client, "get_variance_risk_premium", None)
    if callable(fetch_vrp):
        try:
            resp = fetch_vrp(ticker, timespan="1y") or {}
            rows = resp.get("data") if isinstance(resp, dict) else None
            if isinstance(rows, list) and rows:
                parsed: list[float] = []
                for row in rows:
                    v = row.get("vrp") or row.get("value")
                    if v is None:
                        continue
                    try:
                        parsed.append(float(v))
                    except (TypeError, ValueError):
                        continue
                if parsed:
                    vrp_history = parsed
        except Exception as exc:  # noqa: BLE001
            logger.debug("variance_risk_premium failed for %s: %s", ticker, exc)

    # OI changes — v1 does not use historical OI (Short Squeeze / OI Buildup deferred).
    oi_changes = None

    # PCR — derived from flow_alerts call/put counts (no extra fetch).
    pcr: Optional[float] = None
    if flow_alerts:
        calls = sum(1 for a in flow_alerts if str(a.get("option_type", "")).lower() == "call"
                    or a.get("is_call") is True)
        puts = sum(1 for a in flow_alerts if str(a.get("option_type", "")).lower() == "put"
                   or a.get("is_put") is True)
        if calls > 0:
            pcr = puts / calls

    return TickerData(
        ticker=ticker,
        price=price,
        fetched_at=fetched_at,
        gex=gex,
        gex_by_strike=gex_by_strike,
        iv=iv,
        rv=rv,
        iv_percentile=iv_percentile,
        term_structure=term_structure,
        rr_skew_25d=rr_skew_25d,
        vrp_history=None,  # Task 3 wires this if endpoint probe was 200
        flow_alerts=flow_alerts,
        net_premium=None,  # separate endpoint, not needed for v1 signals
        pcr=pcr,           # derived above from flow_alerts call/put counts
        darkpool=darkpool,
        oi_changes=oi_changes,
        short_interest=short_interest,
        earnings_date=earnings_date,
        earnings_within_14d=earnings_within_14d,
    )
```

- [ ] **Step 4: Run the test, verify it passes**

```bash
pytest scripts/tests/test_analysis_ticker_data.py -v
```
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/analysis/ticker_data.py scripts/tests/test_analysis_ticker_data.py
git commit -m "feat(analysis): TickerData aggregator with iv_rank normalization"
```

---

## Task 3: VRP state + regime classifier

**Files:**
- Create: `scripts/analysis/vrp.py`
- Test: `scripts/tests/test_analysis_vrp.py`
- (Conditional) Modify: `scripts/clients/uw_client.py` — add `get_variance_risk_premium` if Task 0 probe was 200

- [ ] **Step 1: (Conditional) If Task 0 probe was 200, add the VRP wrapper**

Read `docs/superpowers/plans/notes/2026-04-07-vrp-endpoint-probe.md`. If it says AVAILABLE, add to `scripts/clients/uw_client.py` right after `get_volatility_stats` (around line 494):

```python
    def get_variance_risk_premium(self, ticker: str, *, timespan: str = "1y") -> dict:
        """GET /api/volatility/variance_risk_premium/{ticker} - Trailing VRP history."""
        return self._get(
            f"volatility/variance_risk_premium/{ticker.upper()}",
            params={"timespan": timespan},
        )
```

If probe was UNAVAILABLE, skip this step — `vrp_history` stays None everywhere and the regime classifier's null-handling path handles it.

- [ ] **Step 2: Write the failing test**

```python
# scripts/tests/test_analysis_vrp.py
from datetime import datetime
from scripts.analysis.models import TickerData
from scripts.analysis.vrp import build_vrp_state, classify_regime


def _td(**kwargs):
    defaults = dict(
        ticker="X", price=100.0, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=None, rv=None, iv_percentile=None, term_structure=None,
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=None, net_premium=None, pcr=None, darkpool=None,
        oi_changes=None, short_interest=None,
        earnings_date=None, earnings_within_14d=False,
    )
    defaults.update(kwargs)
    return TickerData(**defaults)


def test_vrp_raw_when_iv_and_rv_present():
    td = _td(iv=30.0, rv=22.0)
    s = build_vrp_state(td)
    assert s.vrp_raw == 8.0


def test_vrp_raw_none_when_iv_missing():
    td = _td(iv=None, rv=22.0)
    s = build_vrp_state(td)
    assert s.vrp_raw is None


def test_vrp_zscore_none_when_history_missing():
    td = _td(iv=30.0, rv=22.0, vrp_history=None)
    s = build_vrp_state(td)
    assert s.vrp_zscore is None


def test_vrp_zscore_computed_from_history():
    history = [0.0] * 250 + [8.0]  # mean ~0.032, std ~0.503
    td = _td(iv=30.0, rv=22.0, vrp_history=history)
    s = build_vrp_state(td)
    assert s.vrp_zscore is not None
    assert s.vrp_zscore > 5  # last value is a massive outlier


def test_ts_ratio_from_term_structure():
    term = [
        {"dte": 14, "iv": "0.30"},
        {"dte": 60, "iv": "0.28"},
        {"dte": 90, "iv": "0.27"},
    ]
    td = _td(iv=30.0, rv=22.0, term_structure=term)
    s = build_vrp_state(td)
    # near = 14 DTE (0.30), far = closest to 90 (90 itself, 0.27)
    assert s.ts_ratio is not None
    assert abs(s.ts_ratio - (0.30 / 0.27)) < 1e-6
    assert s.ts_inverted is True  # 0.30/0.27 > 1.05


def test_ts_ratio_none_with_single_expiry():
    td = _td(iv=30.0, rv=22.0, term_structure=[{"dte": 30, "iv": "0.3"}])
    s = build_vrp_state(td)
    assert s.ts_ratio is None
    assert s.ts_inverted is None


def test_regime_r2_when_ts_inverted_and_vrp_negative():
    td = _td(iv=30.0, rv=35.0, gex={"net": -1e9},
             term_structure=[{"dte": 14, "iv": "0.35"}, {"dte": 90, "iv": "0.30"}])
    vrp = build_vrp_state(td)
    # force vrp_zscore negative manually
    from dataclasses import replace
    vrp = replace(vrp, vrp_zscore=-1.0)
    r = classify_regime(td, vrp)
    assert r.regime == "R2"


def test_regime_r1_default_when_signals_mixed():
    td = _td(iv=30.0, rv=22.0, gex={"net": 1e9},
             term_structure=[{"dte": 14, "iv": "0.30"}, {"dte": 90, "iv": "0.29"}])
    vrp = build_vrp_state(td)
    r = classify_regime(td, vrp)
    assert r.regime in ("R0", "R1")  # depends on vrp_zscore None handling


def test_regime_r0_requires_positive_gex_and_elevated_vrp():
    td = _td(iv=30.0, rv=22.0, price=100.0,
             gex={"net": 1e9, "flip": 95.0},  # flip 5% below price
             term_structure=[{"dte": 14, "iv": "0.30"}, {"dte": 90, "iv": "0.31"}])
    vrp = build_vrp_state(td)
    from dataclasses import replace
    vrp = replace(vrp, vrp_zscore=1.2)
    r = classify_regime(td, vrp)
    assert r.regime == "R0"
    assert r.gex_flip_relative == "below_price"
    assert r.flip_distance_pct == 5.0  # magnitude, not signed


def test_regime_flip_distance_is_magnitude_not_signed():
    """flip_distance_pct must be |signed_pct|, direction lives in gex_flip_relative."""
    td_above = _td(iv=30.0, rv=22.0, price=100.0, gex={"net": -1e9, "flip": 103.0})
    td_below = _td(iv=30.0, rv=22.0, price=100.0, gex={"net": -1e9, "flip": 97.0})
    vrp_above = build_vrp_state(td_above)
    vrp_below = build_vrp_state(td_below)
    r_above = classify_regime(td_above, vrp_above)
    r_below = classify_regime(td_below, vrp_below)
    assert r_above.flip_distance_pct == 3.0
    assert r_below.flip_distance_pct == 3.0  # both magnitudes equal
    assert r_above.gex_flip_relative == "above_price"
    assert r_below.gex_flip_relative == "below_price"


def test_regime_r2_on_negative_gex_with_flip_below_price_beyond_2pct():
    """PLAN-11 fix: R2 triggers on flip >2% BELOW price too (not just above)."""
    td = _td(iv=30.0, rv=22.0, price=100.0,
             gex={"net": -5e9, "flip": 93.0},  # 7% below
             term_structure=None)
    vrp = build_vrp_state(td)
    # vrp_zscore is None, flip_dist=7.0>2.0, net_gex<0 → Rule 2 R2
    r = classify_regime(td, vrp)
    assert r.regime == "R2"


def test_regime_biases_toward_r1_when_vrp_unknown():
    """vrp_zscore None must never classify as R0 (the most bullish regime)."""
    td = _td(iv=30.0, rv=22.0, price=100.0,
             gex={"net": 1e9, "flip": 95.0},
             term_structure=[{"dte": 14, "iv": "0.30"}, {"dte": 90, "iv": "0.31"}])
    vrp = build_vrp_state(td)  # vrp_zscore is None
    r = classify_regime(td, vrp)
    assert r.regime != "R0"
```

- [ ] **Step 3: Run the test, verify it fails**

```bash
pytest scripts/tests/test_analysis_vrp.py -v
```
Expected: `ModuleNotFoundError: No module named 'scripts.analysis.vrp'`

- [ ] **Step 4: Create `scripts/analysis/vrp.py`**

```python
# scripts/analysis/vrp.py
"""VRP state builder and regime classifier (R0/R1/R2).

Pure functions: take a TickerData, return a VRPState or RegimeState.
No I/O. No stateful fallbacks. If VRP history is unavailable, vrp_zscore
is None and the regime classifier biases toward R1 (never R0).
"""
from __future__ import annotations

import statistics
from typing import Optional

from scripts.analysis.models import RegimeState, TickerData, VRPState


def _parse_iv(raw) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(raw) * 100.0  # term structure IVs are 0..1 fractions
    except (TypeError, ValueError):
        return None


def _compute_ts_ratio(term_structure: list[dict]) -> tuple[Optional[float], Optional[bool]]:
    """Return (ts_ratio, ts_inverted) from per-expiry IV data.

    near_iv = first expiry with DTE > 7
    far_iv  = expiry closest to 90 DTE
    ts_ratio = near_iv / far_iv
    ts_inverted = ratio > 1.05
    """
    parsed: list[tuple[int, float]] = []
    for row in term_structure:
        dte = row.get("dte") or row.get("days") or row.get("DTE")
        iv = _parse_iv(row.get("iv") or row.get("IV"))
        if dte is None or iv is None:
            continue
        try:
            parsed.append((int(dte), iv))
        except (TypeError, ValueError):
            continue

    near_candidates = [p for p in parsed if p[0] > 7]
    if len(near_candidates) < 2:
        return None, None

    near = min(near_candidates, key=lambda p: p[0])
    far = min(parsed, key=lambda p: abs(p[0] - 90))

    if near == far or far[1] == 0:
        return None, None

    ratio = near[1] / far[1]
    return ratio, ratio > 1.05


def _zscore(history: list[float], current: float) -> Optional[float]:
    if not history or len(history) < 10:
        return None
    try:
        mean = statistics.mean(history)
        std = statistics.stdev(history) if len(history) > 1 else 0.0
    except statistics.StatisticsError:
        return None
    return (current - mean) / max(std, 0.01)


def build_vrp_state(td: TickerData) -> VRPState:
    vrp_raw = None
    if td.iv is not None and td.rv is not None:
        vrp_raw = td.iv - td.rv

    vrp_zscore = None
    if vrp_raw is not None and td.vrp_history:
        vrp_zscore = _zscore(td.vrp_history, vrp_raw)

    ts_ratio, ts_inverted = (None, None)
    if td.term_structure:
        ts_ratio, ts_inverted = _compute_ts_ratio(td.term_structure)

    if td.iv is None:
        freshness = "unavailable"
    elif td.vrp_history is None or vrp_zscore is None:
        freshness = "stale"
    else:
        freshness = "live"

    return VRPState(
        vrp_raw=vrp_raw,
        vrp_zscore=vrp_zscore,
        iv_percentile=td.iv_percentile,
        ts_ratio=ts_ratio,
        ts_inverted=ts_inverted,
        earnings_within_14d=td.earnings_within_14d,
        data_freshness=freshness,
    )


def _net_gex(gex: dict) -> Optional[float]:
    if not gex:
        return None
    for key in ("net", "net_gamma", "total", "value"):
        v = gex.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def _flip_distance(gex: dict, price: Optional[float]) -> tuple[Optional[float], Optional[str]]:
    """Return (magnitude_pct, relative) where relative is above/below/at.

    magnitude_pct is always non-negative (abs value); direction is in `relative`.
    This separation matches the RegimeState model — distance is magnitude,
    direction is a categorical field.
    """
    if not gex or price is None or price == 0:
        return None, None
    flip = gex.get("flip") or gex.get("flip_point") or gex.get("gamma_flip")
    if flip is None:
        return None, None
    try:
        signed = (float(flip) - price) / price * 100.0
    except (TypeError, ValueError):
        return None, None
    magnitude = abs(signed)
    if signed > 0.5:
        relative = "above_price"
    elif signed < -0.5:
        relative = "below_price"
    else:
        relative = "at_price"
    return magnitude, relative


def classify_regime(td: TickerData, vrp: VRPState) -> RegimeState:
    """Classify regime R0/R1/R2 from GEX, term structure, and VRP state.

    Rules (in order):
      R2  if ts_inverted AND vrp_zscore<0
      R2  if net_gex<0 AND flip_distance>2% AND (vrp_zscore is None OR vrp_zscore<0.3)
      R1  if ts_inverted OR (vrp_zscore is not None AND vrp_zscore<0.3)
      R0  if flip_relative=below_price AND vrp_zscore is not None AND vrp_zscore>0.5
      R1  otherwise (cautious default; also the path for vrp_zscore=None)
    """
    net_gex = _net_gex(td.gex) if td.gex else None
    flip_dist, gex_flip_relative = (
        _flip_distance(td.gex, td.price) if td.gex else (None, None)
    )

    if net_gex is None:
        gex_sign = None
    elif net_gex > 0:
        gex_sign = "positive"
    elif net_gex < 0:
        gex_sign = "negative"
    else:
        gex_sign = "mixed"

    # Rule 1: R2 — ts inverted + VRP negative
    if vrp.ts_inverted is True and vrp.vrp_zscore is not None and vrp.vrp_zscore < 0:
        return RegimeState(
            regime="R2", reason="Term structure inverted + VRP negative",
            gex_sign=gex_sign, gex_flip_relative=gex_flip_relative,
            flip_distance_pct=flip_dist,
        )

    # Rule 2: R2 — deeply negative GEX + flip >2% away from price (in either direction)
    # + thin/unknown VRP.
    if (
        net_gex is not None and net_gex < 0
        and flip_dist is not None and flip_dist > 2.0
        and (vrp.vrp_zscore is None or vrp.vrp_zscore < 0.3)
    ):
        return RegimeState(
            regime="R2", reason="Deeply negative GEX + thin/unknown VRP",
            gex_sign=gex_sign, gex_flip_relative=gex_flip_relative,
            flip_distance_pct=flip_dist,
        )

    # Rule 3: R1 — cautionary
    if vrp.ts_inverted is True or (
        vrp.vrp_zscore is not None and vrp.vrp_zscore < 0.3
    ):
        reason = "Caution: inverted TS" if vrp.ts_inverted else "Caution: thin VRP"
        return RegimeState(
            regime="R1", reason=reason,
            gex_sign=gex_sign, gex_flip_relative=gex_flip_relative,
            flip_distance_pct=flip_dist,
        )

    # Rule 4: R0 — positive regime (requires known, elevated VRP)
    if (
        gex_flip_relative == "below_price"
        and vrp.vrp_zscore is not None and vrp.vrp_zscore > 0.5
    ):
        return RegimeState(
            regime="R0", reason="Positive GEX + elevated VRP",
            gex_sign=gex_sign, gex_flip_relative=gex_flip_relative,
            flip_distance_pct=flip_dist,
        )

    # Rule 5: cautious default — this path also catches vrp_zscore=None
    return RegimeState(
        regime="R1", reason="Mixed signals",
        gex_sign=gex_sign, gex_flip_relative=gex_flip_relative,
        flip_distance_pct=flip_dist,
    )
```

- [ ] **Step 5: Run the test, verify it passes**

```bash
pytest scripts/tests/test_analysis_vrp.py -v
```
Expected: all 10 tests PASS. If `test_regime_r0_requires_positive_gex_and_elevated_vrp` fails because your fixture's `flip=95` with `price=100` doesn't classify as below_price, check the `_flip_distance` sign: `(95-100)/100*100 = -5` → below_price (flip_dist < -0.5). Adjust if needed.

- [ ] **Step 6: Commit**

```bash
git add scripts/analysis/vrp.py scripts/tests/test_analysis_vrp.py
if [ -n "$(git status --porcelain scripts/clients/uw_client.py)" ]; then
  git add scripts/clients/uw_client.py
fi
git commit -m "feat(analysis): VRP state + R0/R1/R2 regime classifier"
```

---

## Task 4: GEX helpers (flip, walls, pinning)

**Files:**
- Create: `scripts/analysis/gex.py`
- Test: `scripts/tests/test_analysis_gex.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_analysis_gex.py
from datetime import date
from scripts.analysis.gex import detect_flip_point, rank_walls, detect_pinning, is_opex_week


def test_detect_flip_point_finds_sign_change():
    strikes = [
        {"strike": 90, "gamma": -1.0},
        {"strike": 95, "gamma": -0.5},
        {"strike": 100, "gamma": 0.3},
        {"strike": 105, "gamma": 1.2},
    ]
    flip = detect_flip_point(strikes)
    assert flip is not None
    assert 95 <= flip <= 100


def test_detect_flip_point_none_when_all_positive():
    strikes = [{"strike": 100, "gamma": 1.0}, {"strike": 105, "gamma": 2.0}]
    assert detect_flip_point(strikes) is None


def test_rank_walls_returns_top_absolute_gamma():
    strikes = [
        {"strike": 100, "gamma": 0.5},
        {"strike": 105, "gamma": -2.0},
        {"strike": 110, "gamma": 1.8},
        {"strike": 115, "gamma": 0.1},
    ]
    walls = rank_walls(strikes, top_n=2)
    assert len(walls) == 2
    assert walls[0]["strike"] == 105  # abs=2.0
    assert walls[1]["strike"] == 110  # abs=1.8


def test_detect_pinning_flags_near_wall_in_opex_week():
    strikes = [{"strike": 100, "gamma": 5.0}, {"strike": 105, "gamma": 0.1}]
    result = detect_pinning(strikes, price=100.5, opex_week=True, min_gamma=1.0)
    assert result is not None
    assert result["pin_strike"] == 100


def test_detect_pinning_none_outside_opex_week():
    strikes = [{"strike": 100, "gamma": 5.0}]
    assert detect_pinning(strikes, price=100.5, opex_week=False, min_gamma=1.0) is None


def test_is_opex_week_third_friday_and_three_days_before():
    # April 2026: 3rd Friday is April 17
    assert is_opex_week(date(2026, 4, 17)) is True
    assert is_opex_week(date(2026, 4, 15)) is True  # 2 days before
    assert is_opex_week(date(2026, 4, 14)) is True  # 3 days before
    assert is_opex_week(date(2026, 4, 13)) is False  # 4 days before
    assert is_opex_week(date(2026, 4, 20)) is False  # after
```

- [ ] **Step 2: Run the test, verify it fails**

```bash
pytest scripts/tests/test_analysis_gex.py -v
```
Expected: `ModuleNotFoundError: No module named 'scripts.analysis.gex'`

- [ ] **Step 3: Create `scripts/analysis/gex.py`**

```python
# scripts/analysis/gex.py
"""GEX helpers: flip point, wall ranking, opex pinning detection."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional


def _gamma(row: dict) -> Optional[float]:
    for key in ("gamma", "net_gamma", "total_gamma", "value"):
        v = row.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return None


def detect_flip_point(strikes: list[dict]) -> Optional[float]:
    """Find the strike where net gamma transitions from negative to positive.

    Returns the midpoint between the last negative and first positive strike.
    Returns None if no transition exists (all same sign).
    """
    sorted_strikes = sorted(
        (s for s in strikes if _gamma(s) is not None and s.get("strike") is not None),
        key=lambda s: float(s["strike"]),
    )
    if len(sorted_strikes) < 2:
        return None

    prev = sorted_strikes[0]
    for curr in sorted_strikes[1:]:
        prev_g = _gamma(prev)
        curr_g = _gamma(curr)
        if prev_g is None or curr_g is None:
            prev = curr
            continue
        if prev_g < 0 and curr_g >= 0:
            return (float(prev["strike"]) + float(curr["strike"])) / 2.0
        prev = curr
    return None


def rank_walls(strikes: list[dict], top_n: int = 3) -> list[dict]:
    """Return the top N strikes by absolute gamma magnitude."""
    scored = [
        {**s, "_abs_gamma": abs(_gamma(s) or 0.0)}
        for s in strikes
        if _gamma(s) is not None
    ]
    scored.sort(key=lambda s: s["_abs_gamma"], reverse=True)
    return [{k: v for k, v in s.items() if k != "_abs_gamma"} for s in scored[:top_n]]


def detect_pinning(
    strikes: list[dict],
    *,
    price: float,
    opex_week: bool,
    min_gamma: float = 1.0,
    max_distance_pct: float = 1.0,
) -> Optional[dict]:
    """Detect GEX pinning: large gamma near current price during opex week.

    Returns {"pin_strike", "gamma", "distance_pct"} or None.
    """
    if not opex_week or price <= 0:
        return None

    for wall in rank_walls(strikes, top_n=5):
        strike = float(wall.get("strike") or 0)
        gamma = _gamma(wall) or 0.0
        if abs(gamma) < min_gamma:
            continue
        distance_pct = abs(strike - price) / price * 100.0
        if distance_pct <= max_distance_pct:
            return {
                "pin_strike": strike,
                "gamma": gamma,
                "distance_pct": distance_pct,
            }
    return None


def is_opex_week(today: date) -> bool:
    """True if today is within 3 calendar days of the 3rd Friday of the month."""
    first_day = today.replace(day=1)
    # Friday is weekday 4
    first_friday_offset = (4 - first_day.weekday()) % 7
    third_friday = first_day + timedelta(days=first_friday_offset + 14)
    delta = (third_friday - today).days
    return 0 <= delta <= 3 or today == third_friday
```

- [ ] **Step 4: Run the test, verify it passes**

```bash
pytest scripts/tests/test_analysis_gex.py -v
```
Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/analysis/gex.py scripts/tests/test_analysis_gex.py
git commit -m "feat(analysis): GEX flip/walls/pinning detection helpers"
```

---

## Task 5: Benchmark context loader (SPY + sector ETF)

**Files:**
- Create: `scripts/analysis/benchmark.py`
- Test: `scripts/tests/test_analysis_benchmark.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_analysis_benchmark.py
from unittest.mock import MagicMock
from scripts.analysis.benchmark import load_benchmark_context, SECTOR_ETF_MAP


def test_sector_etf_map_has_common_sectors():
    assert "Technology" in SECTOR_ETF_MAP
    assert SECTOR_ETF_MAP["Technology"] == "XLK"


def test_load_benchmark_context_spy_only_when_sector_unknown():
    client = MagicMock()
    client.get_volatility_stats.return_value = {"iv_rank": "0.45"}
    client.get_greek_exposure.return_value = {"net": 1e9, "flip": 450.0, "price": 460.0}
    client.get_stock_info.return_value = {}

    ctx = load_benchmark_context(client, ticker_sector=None)
    assert ctx.spy.ticker == "SPY"
    assert ctx.sector_etf is None


def test_load_benchmark_context_with_sector_etf():
    client = MagicMock()
    def vs(ticker):
        return {"iv_rank": "0.40" if ticker == "SPY" else "0.55"}
    def ge(ticker):
        return {"net": 1e9, "flip": 100.0, "price": 105.0}
    client.get_volatility_stats.side_effect = vs
    client.get_greek_exposure.side_effect = ge

    ctx = load_benchmark_context(client, ticker_sector="Technology")
    assert ctx.spy.ticker == "SPY"
    assert ctx.sector_etf is not None
    assert ctx.sector_etf.ticker == "XLK"


def test_load_benchmark_context_degrades_on_spy_fetch_failure():
    client = MagicMock()
    client.get_volatility_stats.side_effect = Exception("boom")
    client.get_greek_exposure.side_effect = Exception("boom")

    ctx = load_benchmark_context(client, ticker_sector=None)
    assert ctx.spy.freshness == "unavailable"
```

- [ ] **Step 2: Run the test, verify it fails**

```bash
pytest scripts/tests/test_analysis_benchmark.py -v
```
Expected: module not found.

- [ ] **Step 3: Create `scripts/analysis/benchmark.py`**

```python
# scripts/analysis/benchmark.py
"""SPY + sector ETF benchmark loader."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from scripts.analysis.models import BenchmarkContext, BenchmarkSnapshot

logger = logging.getLogger(__name__)


SECTOR_ETF_MAP: dict[str, str] = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Financials": "XLF",
    "Healthcare": "XLV",
    "Consumer Cyclical": "XLY",
    "Consumer Defensive": "XLP",
    "Communication Services": "XLC",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Basic Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
}


def _to_pct(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v) * 100.0
    except (TypeError, ValueError):
        return None


def _to_float(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _load_snapshot(client, ticker: str) -> BenchmarkSnapshot:
    iv_rank = None
    gex_regime = None
    gex_flip = None
    price = None
    ok = True

    try:
        vol = client.get_volatility_stats(ticker) or {}
        iv_rank = _to_pct(vol.get("iv_rank"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("benchmark vol_stats failed for %s: %s", ticker, exc)
        ok = False

    try:
        gex = client.get_greek_exposure(ticker) or {}
        net = gex.get("net") or gex.get("net_gamma")
        if net is not None:
            net_f = _to_float(net) or 0.0
            if net_f > 0:
                gex_regime = "positive"
            elif net_f < 0:
                gex_regime = "negative"
            else:
                gex_regime = "mixed"
        gex_flip = _to_float(gex.get("flip") or gex.get("flip_point"))
        price = _to_float(gex.get("price") or gex.get("spot"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("benchmark gex failed for %s: %s", ticker, exc)
        ok = False

    freshness = "live" if ok else "unavailable"
    return BenchmarkSnapshot(
        ticker=ticker,
        iv_rank=iv_rank,
        gex_regime=gex_regime,
        gex_flip=gex_flip,
        price=price,
        data_date=datetime.now().strftime("%Y-%m-%d"),
        freshness=freshness,
    )


def load_benchmark_context(client, *, ticker_sector: Optional[str] = None) -> BenchmarkContext:
    """Load SPY + optional sector ETF snapshots."""
    spy = _load_snapshot(client, "SPY")
    sector_etf: Optional[BenchmarkSnapshot] = None
    if ticker_sector and ticker_sector in SECTOR_ETF_MAP:
        etf_symbol = SECTOR_ETF_MAP[ticker_sector]
        sector_etf = _load_snapshot(client, etf_symbol)
    return BenchmarkContext(spy=spy, sector_etf=sector_etf)
```

- [ ] **Step 4: Run the test, verify it passes**

```bash
pytest scripts/tests/test_analysis_benchmark.py -v
```
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/analysis/benchmark.py scripts/tests/test_analysis_benchmark.py
git commit -m "feat(analysis): SPY + sector ETF benchmark loader"
```

---

## Task 6: 4-bucket scoring with reweighting and fast mode

**Files:**
- Create: `scripts/analysis/scoring.py`
- Test: `scripts/tests/test_analysis_scoring.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_analysis_scoring.py
from datetime import datetime
from scripts.analysis.models import TickerData, VRPState, RegimeState
from scripts.analysis.scoring import score_buckets, BUCKET_WEIGHTS


def _td(**kwargs):
    defaults = dict(
        ticker="X", price=100.0, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=30.0, rv=22.0, iv_percentile=60.0,
        term_structure=[{"dte": 14, "iv": "0.30"}, {"dte": 90, "iv": "0.28"}],
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=[], net_premium=None, pcr=1.0, darkpool=None,
        oi_changes=[], short_interest=None,
        earnings_date=None, earnings_within_14d=False,
    )
    defaults.update(kwargs)
    return TickerData(**defaults)


_VRP = VRPState(
    vrp_raw=8.0, vrp_zscore=1.0, iv_percentile=60.0,
    ts_ratio=1.07, ts_inverted=True, earnings_within_14d=False,
    data_freshness="live",
)
_REGIME = RegimeState(
    regime="R1", reason="test",
    gex_sign="positive", gex_flip_relative="below_price", flip_distance_pct=1.0,
)


def test_bucket_weights_sum_to_100():
    assert sum(BUCKET_WEIGHTS.values()) == 100


def test_full_mode_composite_in_range():
    td = _td(
        gex={"net": 1e9, "flip": 95.0},
        gex_by_strike={"strikes": []},
    )
    scores = score_buckets(td, _VRP, _REGIME, mode="full")
    assert scores.mode == "full"
    assert -100 <= scores.composite <= 100
    assert scores.bias in (
        "STRONGLY_BULLISH", "BULLISH", "MIXED", "BEARISH", "STRONGLY_BEARISH"
    )


def test_fast_mode_skips_flow_and_positioning():
    td = _td(gex={"net": 1e9}, gex_by_strike={"strikes": []})
    scores = score_buckets(td, _VRP, _REGIME, mode="fast")
    assert scores.mode == "fast"
    assert scores.reweighted is True
    # fast mode explicitly skips flow + positioning; positioning is also
    # always skipped due to v1 limitation — set must contain both.
    assert "flow" in scores.skipped_buckets
    assert "positioning" in scores.skipped_buckets


def test_fast_mode_caps_grade_at_b():
    td = _td(gex={"net": 1e9}, gex_by_strike={"strikes": []})
    scores = score_buckets(td, _VRP, _REGIME, mode="fast")
    assert scores.grade in ("B", "C")  # cannot be A in fast mode


def test_bucket_failure_reweights():
    """When a bucket is unavailable due to missing data, reweight to ±100."""
    td = _td(gex=None, gex_by_strike=None)  # market_structure unavailable
    scores = score_buckets(td, _VRP, _REGIME, mode="full")
    assert "market_structure" in scores.skipped_buckets
    # Positioning is always skipped in v1 (known limitation)
    assert "positioning" in scores.skipped_buckets
    assert scores.reweighted is True


def test_bias_mapping_boundaries():
    """Score 60 → STRONGLY_BULLISH, 20 → BULLISH, -20 → BEARISH."""
    from scripts.analysis.scoring import score_to_bias
    assert score_to_bias(75.0) == "STRONGLY_BULLISH"
    assert score_to_bias(60.0) == "STRONGLY_BULLISH"
    assert score_to_bias(59.9) == "BULLISH"
    assert score_to_bias(20.0) == "BULLISH"
    assert score_to_bias(19.9) == "MIXED"
    assert score_to_bias(0.0) == "MIXED"
    assert score_to_bias(-19.9) == "MIXED"
    assert score_to_bias(-20.0) == "BEARISH"
    assert score_to_bias(-60.0) == "STRONGLY_BEARISH"
```

- [ ] **Step 2: Run the test, verify it fails**

```bash
pytest scripts/tests/test_analysis_scoring.py -v
```
Expected: module not found.

- [ ] **Step 3: Create `scripts/analysis/scoring.py`**

```python
# scripts/analysis/scoring.py
"""4-bucket composite scoring with reweighting and fast mode.

Weights and bias mapping are lifted from the source skill unchanged.
Tunable via BUCKET_WEIGHTS constant.
"""
from __future__ import annotations

from typing import Literal

from scripts.analysis.models import BucketScores, RegimeState, TickerData, VRPState


BUCKET_WEIGHTS: dict[str, int] = {
    "market_structure": 28,
    "volatility": 28,
    "flow": 24,
    "positioning": 20,
}

Mode = Literal["full", "fast"]


def score_to_bias(composite: float) -> str:
    if composite >= 60:
        return "STRONGLY_BULLISH"
    if composite >= 20:
        return "BULLISH"
    if composite > -20:
        return "MIXED"
    if composite > -60:
        return "BEARISH"
    return "STRONGLY_BEARISH"


def _score_market_structure(td: TickerData, regime: RegimeState) -> float:
    """Market structure bucket (±28). Simple v1 heuristic."""
    score = 0.0
    if regime.gex_sign == "positive":
        score += 10
    elif regime.gex_sign == "negative":
        score -= 10
    if regime.gex_flip_relative == "below_price":
        score += 8
    elif regime.gex_flip_relative == "above_price":
        score -= 8
    # Clamp to ±28
    return max(-28.0, min(28.0, score))


def _score_volatility(td: TickerData, vrp: VRPState) -> float:
    """Volatility bucket (±28)."""
    score = 0.0
    if vrp.iv_percentile is not None:
        # High IV rank is bearish for longs (expensive premium)
        if vrp.iv_percentile > 75:
            score -= 6
        elif vrp.iv_percentile < 30:
            score += 6
    if vrp.vrp_zscore is not None:
        if vrp.vrp_zscore > 1.0:
            score += 8
        elif vrp.vrp_zscore < 0:
            score -= 8
    if vrp.ts_inverted is True:
        score -= 10  # backwardation = event risk
    return max(-28.0, min(28.0, score))


def _score_flow(td: TickerData) -> float:
    """Flow bucket (±24).

    v1 LIMITATION: Uses flow alerts count + PCR as directional proxies.
    Net-premium, dark-pool conviction, and call/put premium-weighted signals
    are deferred to a follow-up spec that wires the richer endpoints.
    """
    if not td.flow_alerts:
        return 0.0
    n = len(td.flow_alerts)
    score = min(12.0, n * 1.5)  # max 12 from volume
    # PCR directional bias: extreme fear = contrarian bullish
    if td.pcr is not None:
        if td.pcr > 1.5:
            score += 8   # extreme fear → contrarian bullish
        elif td.pcr > 1.2:
            score += 4   # elevated fear → mildly bullish
        elif td.pcr < 0.5:
            score -= 6   # complacent → caution
    return max(-24.0, min(24.0, score))


def _score_positioning(td: TickerData) -> float:
    """Positioning bucket (±20).

    v1 LIMITATION: OI change bias requires historical OI that Xenon does not
    persist today. Short interest is also T+1 and optional. This bucket
    returns 0 in v1 and is marked as skipped in test assertions. It will be
    implemented in the same follow-up spec that adds OI Buildup / Short
    Squeeze signals.
    """
    return 0.0


def _grade(available_buckets: int, has_confluence: bool) -> Literal["A", "B", "C"]:
    if available_buckets >= 3 and has_confluence:
        return "A"
    if available_buckets >= 2:
        return "B"
    return "C"


def score_buckets(
    td: TickerData, vrp: VRPState, regime: RegimeState, *, mode: Mode = "full"
) -> BucketScores:
    """Compute 4-bucket composite + bias + grade.

    Missing-data and fast-mode both use the same reweighting formula but are
    tracked separately via `mode` and `skipped_buckets`.
    """
    raw: dict[str, float] = {}
    skipped: list[str] = []

    # Market Structure
    if td.bucket_available("market_structure"):
        raw["market_structure"] = _score_market_structure(td, regime)
    else:
        skipped.append("market_structure")
        raw["market_structure"] = 0.0

    # Volatility
    if td.bucket_available("volatility"):
        raw["volatility"] = _score_volatility(td, vrp)
    else:
        skipped.append("volatility")
        raw["volatility"] = 0.0

    # Flow (fast mode skips)
    if mode == "fast":
        raw["flow"] = 0.0
        if "flow" not in skipped:
            skipped.append("flow")
    elif td.bucket_available("flow"):
        raw["flow"] = _score_flow(td)
    else:
        skipped.append("flow")
        raw["flow"] = 0.0

    # Positioning (fast mode skips)
    if mode == "fast":
        raw["positioning"] = 0.0
        if "positioning" not in skipped:
            skipped.append("positioning")
    elif td.bucket_available("positioning"):
        raw["positioning"] = _score_positioning(td)
    else:
        skipped.append("positioning")
        raw["positioning"] = 0.0

    # Reweight
    available_max = sum(
        w for name, w in BUCKET_WEIGHTS.items() if name not in skipped
    )
    reweighted = bool(skipped)
    if available_max <= 0:
        composite = 0.0
    else:
        raw_sum = sum(
            raw[name] for name in BUCKET_WEIGHTS if name not in skipped
        )
        composite = raw_sum * (100.0 / available_max)

    composite = max(-100.0, min(100.0, composite))
    bias = score_to_bias(composite)

    available_count = sum(
        1 for name in BUCKET_WEIGHTS if name not in skipped
    )
    has_confluence = abs(composite) >= 40
    grade = _grade(available_count, has_confluence)
    if mode == "fast" and grade == "A":
        grade = "B"  # cap at B in fast mode

    return BucketScores(
        market_structure=raw["market_structure"],
        volatility=raw["volatility"],
        flow=raw["flow"],
        positioning=raw["positioning"],
        composite=composite,
        grade=grade,
        bias=bias,  # type: ignore[arg-type]
        mode=mode,
        reweighted=reweighted,
        skipped_buckets=skipped,
    )
```

- [ ] **Step 4: Run the test, verify it passes**

```bash
pytest scripts/tests/test_analysis_scoring.py -v
```
Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/analysis/scoring.py scripts/tests/test_analysis_scoring.py
git commit -m "feat(analysis): 4-bucket composite scoring with reweighting + fast mode"
```

---

## Task 7: Context gates (earnings, liquidity, regime)

**Files:**
- Create: `scripts/analysis/gates.py`
- Test: `scripts/tests/test_analysis_gates.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_analysis_gates.py
from scripts.analysis.gates import earnings_gate, liquidity_gate, regime_gate


def test_earnings_gate_blocks_within_window():
    assert earnings_gate(earnings_within_14d=True, window_days=14) is False
    assert earnings_gate(earnings_within_14d=False, window_days=14) is True


def test_earnings_gate_respects_custom_window():
    # Even "within 14d" callers can use a tighter window — but v1 only
    # tracks within_14d, so shorter-window callers get the conservative answer.
    assert earnings_gate(earnings_within_14d=True, window_days=2) is False


def test_liquidity_gate_requires_min_option_volume():
    assert liquidity_gate(option_volume=500, min_volume=1000) is False
    assert liquidity_gate(option_volume=1500, min_volume=1000) is True
    assert liquidity_gate(option_volume=None, min_volume=1000) is False


def test_regime_gate_blocks_r2_only():
    assert regime_gate(regime="R0") is True
    assert regime_gate(regime="R1") is True
    assert regime_gate(regime="R2") is False
```

- [ ] **Step 2: Run the test, verify it fails**

```bash
pytest scripts/tests/test_analysis_gates.py -v
```
Expected: module not found.

- [ ] **Step 3: Create `scripts/analysis/gates.py`**

```python
# scripts/analysis/gates.py
"""Context gates for signal quality.

These are NOT the Four Gates (convexity / edge / Kelly / naked-short).
They are signal-quality filters applied before a signal is emitted.
"""
from __future__ import annotations

from typing import Optional


def earnings_gate(*, earnings_within_14d: bool, window_days: int = 14) -> bool:
    """Pass if no earnings within the specified window.

    v1 only tracks within_14d, so callers requesting a shorter window get
    the conservative answer (treated as 14d).
    """
    return not earnings_within_14d


def liquidity_gate(*, option_volume: Optional[int], min_volume: int = 1000) -> bool:
    """Pass if option volume meets the minimum threshold."""
    if option_volume is None:
        return False
    return option_volume >= min_volume


def regime_gate(*, regime: str) -> bool:
    """Pass unless market regime is R2 (risk-off)."""
    return regime != "R2"
```

- [ ] **Step 4: Run the test, verify it passes**

```bash
pytest scripts/tests/test_analysis_gates.py -v
```
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/analysis/gates.py scripts/tests/test_analysis_gates.py
git commit -m "feat(analysis): context gates (earnings, liquidity, regime)"
```

---

## Task 8: `uw_analyze.run_analysis()` + CLI

**Files:**
- Create: `scripts/uw_analyze.py`
- Test: `scripts/tests/test_uw_analyze.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_uw_analyze.py
import json
from unittest.mock import MagicMock, patch

from scripts.uw_analyze import run_analysis
from scripts.analysis.models import AnalysisReport


def _full_client():
    c = MagicMock()
    c.get_volatility_stats.return_value = {"iv": "0.28", "rv": "0.21", "iv_rank": "0.55"}
    c.get_volatility_term_structure.return_value = {
        "data": [{"dte": 14, "iv": "0.30"}, {"dte": 90, "iv": "0.28"}]
    }
    c.get_greek_exposure.return_value = {"net": 1e9, "flip": 95.0, "price": 100.0}
    c.get_greek_exposure_by_strike.return_value = {"strikes": []}
    c.get_flow_alerts.return_value = {"data": [{}, {}]}
    c.get_darkpool_flow.return_value = {}
    c.get_earnings_by_ticker.return_value = {"data": []}
    c.get_short_data.return_value = {}
    c.get_historical_risk_reversal_skew.return_value = {"data": []}
    c.__enter__ = lambda self: self
    c.__exit__ = lambda self, *a: None
    return c


def test_run_analysis_returns_report():
    client = _full_client()
    report = run_analysis("TSLA", client=client)
    assert isinstance(report, AnalysisReport)
    assert report.ticker == "TSLA"
    assert report.scores.mode == "full"


def test_run_analysis_fast_mode_caps_grade_and_skips_buckets():
    client = _full_client()
    report = run_analysis("TSLA", fast=True, client=client)
    assert report.scores.mode == "fast"
    assert "flow" in report.scores.skipped_buckets
    assert "positioning" in report.scores.skipped_buckets


def test_run_analysis_handles_missing_vol_stats():
    client = _full_client()
    client.get_volatility_stats.return_value = {}
    report = run_analysis("TSLA", client=client)
    assert "volatility" in report.scores.skipped_buckets
    assert report.scores.reweighted is True
```

- [ ] **Step 2: Run the test, verify it fails**

```bash
pytest scripts/tests/test_uw_analyze.py -v
```
Expected: `ModuleNotFoundError: No module named 'scripts.uw_analyze'`

- [ ] **Step 3: Create `scripts/uw_analyze.py`**

```python
# scripts/uw_analyze.py
"""uw-analyze: per-ticker signal analysis CLI + run_analysis() library function.

Primary entry is `run_analysis()`, which is also consumed in-process by
`uw-scan --analyze-top N`. The CLI is a thin wrapper for debug/research.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from scripts.analysis.benchmark import load_benchmark_context
from scripts.analysis.models import AnalysisReport
from scripts.analysis.scoring import score_buckets
from scripts.analysis.ticker_data import fetch_ticker_data
from scripts.analysis.vrp import build_vrp_state, classify_regime
from scripts.clients.uw_client import UWClient


def run_analysis(
    ticker: str,
    *,
    fast: bool = False,
    client: Optional[UWClient] = None,
) -> AnalysisReport:
    """Run the full analysis pipeline for a single ticker.

    Args:
        ticker: ticker symbol (will be uppercased).
        fast: skip Flow + Positioning buckets.
        client: optional existing UWClient to reuse. If None, create and
                close one in a context manager.

    Returns:
        AnalysisReport with VRP state, regime, benchmark context, and bucket scores.
    """
    owns_client = client is None
    if owns_client:
        client = UWClient()

    try:
        td = fetch_ticker_data(ticker, client)
        vrp = build_vrp_state(td)
        regime = classify_regime(td, vrp)
        scores = score_buckets(td, vrp, regime, mode="fast" if fast else "full")
        # Resolve sector for benchmark comparison.
        ticker_sector: Optional[str] = None
        try:
            info = client.get_stock_info(td.ticker) or {}
            ticker_sector = info.get("sector") or info.get("gics_sector")
        except Exception:  # noqa: BLE001
            ticker_sector = None
        benchmark = load_benchmark_context(client, ticker_sector=ticker_sector)

        data_freshness = {
            "gex": "live" if td.gex is not None else "unavailable",
            "volatility": vrp.data_freshness,
            "earnings": "stale" if td.earnings_date is None else "live",
            "benchmark_spy": benchmark.spy.freshness,
        }

        notes: list[str] = []
        if vrp.vrp_zscore is None:
            notes.append("VRP z-score unavailable — regime defaulted to cautious.")
        if scores.reweighted:
            notes.append(
                f"Buckets reweighted due to missing data: {scores.skipped_buckets}"
            )

        return AnalysisReport(
            ticker=td.ticker,
            price=td.price,
            fetched_at=td.fetched_at.isoformat(),
            data_freshness=data_freshness,
            benchmark=benchmark,
            vrp=vrp,
            regime=regime,
            scores=scores,
            notes=notes,
        )
    finally:
        if owns_client:
            client.close()


def _report_to_dict(report: AnalysisReport) -> dict:
    """asdict is not JSON-serializable for date/datetime — handle manually."""
    d = asdict(report)
    # All fields are already strings or primitives; dates were strings in construction.
    return d


def _format_summary(report: AnalysisReport) -> str:
    lines = [
        "=" * 60,
        f"{report.ticker}  @  ${report.price or 'N/A'}",
        f"Fetched: {report.fetched_at}",
        "-" * 60,
        f"Bias:        {report.scores.bias}  (composite {report.scores.composite:+.1f})",
        f"Grade:       {report.scores.grade}  ({report.scores.mode} mode)",
        f"Regime:      {report.regime.regime}  — {report.regime.reason}",
        f"VRP z-score: {report.vrp.vrp_zscore if report.vrp.vrp_zscore is not None else 'unavailable'}",
        f"IV pctl:     {report.vrp.iv_percentile}",
        "-" * 60,
        "Buckets:",
        f"  Market Structure: {report.scores.market_structure:+.1f}",
        f"  Volatility:       {report.scores.volatility:+.1f}",
        f"  Flow:             {report.scores.flow:+.1f}",
        f"  Positioning:      {report.scores.positioning:+.1f}",
    ]
    if report.scores.skipped_buckets:
        lines.append(f"  (skipped: {', '.join(report.scores.skipped_buckets)})")
    if report.notes:
        lines.append("-" * 60)
        lines.append("Notes:")
        for n in report.notes:
            lines.append(f"  - {n}")
    lines.append("=" * 60)
    lines.append("NOTE: This is a signal summary, not a trade recommendation.")
    lines.append("=" * 60)
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        description="uw-analyze: per-ticker UW signal analysis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("tickers", nargs="+", help="Ticker(s) to analyze")
    p.add_argument("--fast", action="store_true", help="Skip Flow + Positioning buckets")
    p.add_argument("--json", action="store_true", help="Print JSON to stdout instead of summary")
    args = p.parse_args(argv)

    out_dir = Path("data/analysis")
    out_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y%m%d")

    # UWClient() construction raises on missing UW_TOKEN — propagate that
    # out of main() so the shell sees non-zero exit for infra/config failure.
    try:
        client_ctx = UWClient()
    except Exception as exc:  # noqa: BLE001
        print(f"UWClient init failed: {exc}", file=sys.stderr)
        return 2

    had_ticker_error = False
    with client_ctx as client:
        for raw_ticker in args.tickers:
            ticker = raw_ticker.upper()
            try:
                report = run_analysis(ticker, fast=args.fast, client=client)
            except Exception as exc:  # noqa: BLE001
                # Ticker-specific errors are logged and skipped (non-fatal).
                print(f"[{ticker}] ERROR: {exc}", file=sys.stderr)
                had_ticker_error = True
                continue

            out_file = out_dir / f"{ticker}-{today}.json"
            out_file.write_text(json.dumps(_report_to_dict(report), default=str, indent=2))

            if args.json:
                print(json.dumps(_report_to_dict(report), default=str, indent=2))
            else:
                print(_format_summary(report))

    # Exit 0 if at least one ticker succeeded; non-zero only on total failure
    # or infrastructure errors (handled above).
    return 0 if not had_ticker_error or len(args.tickers) > 1 else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the test, verify it passes**

```bash
pytest scripts/tests/test_uw_analyze.py -v
```
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/uw_analyze.py scripts/tests/test_uw_analyze.py
git commit -m "feat(uw-analyze): run_analysis() library + thin CLI"
```

---

## Task 9: `uw_scan_lib/` skeleton + models + universe loader

**Files:**
- Create: `scripts/uw_scan_lib/__init__.py`
- Create: `scripts/uw_scan_lib/models.py`
- Create: `scripts/uw_scan_lib/universe.py`
- Test: `scripts/tests/test_uw_scan_models.py`
- Test: `scripts/tests/test_uw_scan_universe.py`

- [ ] **Step 1: Write failing tests**

```python
# scripts/tests/test_uw_scan_models.py
from scripts.uw_scan_lib.models import SignalHit, ContextFlag, ScanCandidate


def test_signal_hit_basic():
    h = SignalHit(
        ticker="TSLA", signal_type="deep_conviction_flow", tier=1, score=0.8,
        evidence={"premium": 2_000_000},
    )
    assert h.ticker == "TSLA"
    assert h.tier == 1


def test_scan_candidate_is_type_f_default_false():
    c = ScanCandidate(
        ticker="TSLA", hits=[], context_flags=[],
        raw_score=0.0, confluence_score=0.0, final_score=0.0,
        is_type_f=False,
    )
    assert c.is_type_f is False
```

```python
# scripts/tests/test_uw_scan_universe.py
import json
from pathlib import Path

from scripts.uw_scan_lib.universe import load_universe


def test_targeted_mode_returns_explicit_list():
    tickers = load_universe(mode="targeted", tickers=["aapl", "MSFT"])
    assert tickers == ["AAPL", "MSFT"]


def test_targeted_mode_deduplicates():
    tickers = load_universe(mode="targeted", tickers=["AAPL", "aapl", "MSFT"])
    assert tickers == ["AAPL", "MSFT"]


def test_watchlist_mode_reads_data_watchlist_json(tmp_path, monkeypatch):
    watch = tmp_path / "watchlist.json"
    watch.write_text(json.dumps({
        "tickers": [
            {"ticker": "AAPL"}, {"ticker": "msft"}, {"ticker": "NVDA"},
        ]
    }))
    monkeypatch.chdir(tmp_path.parent)
    (tmp_path.parent / "data").mkdir(exist_ok=True)
    (tmp_path.parent / "data" / "watchlist.json").write_text(watch.read_text())
    tickers = load_universe(mode="watchlist")
    assert "AAPL" in tickers and "MSFT" in tickers and "NVDA" in tickers
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
pytest scripts/tests/test_uw_scan_models.py scripts/tests/test_uw_scan_universe.py -v
```
Expected: module not found.

- [ ] **Step 3: Create the files**

```python
# scripts/uw_scan_lib/__init__.py
"""uw-scan library: tiered signal scanner for options opportunities."""
```

```python
# scripts/uw_scan_lib/models.py
"""uw-scan dataclasses."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional


@dataclass(frozen=True)
class SignalHit:
    ticker: str
    signal_type: str                  # e.g. "deep_conviction_flow"
    tier: Literal[1, 2]
    score: float                      # 0..1 signal strength
    evidence: dict[str, Any]          # human-readable details
    freshness: Literal["live", "stale", "unavailable"] = "live"


@dataclass(frozen=True)
class ContextFlag:
    ticker: str
    layer: str                        # e.g. "pcr_sentiment"
    label: str                        # e.g. "Elevated Fear"
    value: float
    # Zero numeric weight — context never affects final_score.


@dataclass
class ScanCandidate:
    ticker: str
    hits: list[SignalHit]
    context_flags: list[ContextFlag]
    raw_score: float
    confluence_score: float
    final_score: float
    is_type_f: bool
    gates: dict[str, str] = field(default_factory=dict)
```

```python
# scripts/uw_scan_lib/universe.py
"""Ticker universe loader for uw-scan."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Optional

Mode = Literal["watchlist", "targeted"]


def load_universe(
    *,
    mode: Mode,
    tickers: Optional[list[str]] = None,
    watchlist_path: str = "data/watchlist.json",
) -> list[str]:
    """Load a ticker universe for scanning.

    - watchlist: reads data/watchlist.json (singular — Xenon's existing file).
    - targeted: uses the provided tickers list, uppercased and deduped.
    - market_wide is NOT supported in v1 (see Known v1 limitations).
    """
    if mode == "targeted":
        if not tickers:
            return []
        seen: set[str] = set()
        result: list[str] = []
        for t in tickers:
            up = t.upper()
            if up not in seen:
                seen.add(up)
                result.append(up)
        return result

    if mode == "watchlist":
        path = Path(watchlist_path)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            return []
        raw_tickers = data.get("tickers", [])
        out: list[str] = []
        seen = set()
        for row in raw_tickers:
            if isinstance(row, dict) and row.get("ticker"):
                up = str(row["ticker"]).upper()
            elif isinstance(row, str):
                up = row.upper()
            else:
                continue
            if up not in seen:
                seen.add(up)
                out.append(up)
        return out

    raise ValueError(f"unsupported mode: {mode}")
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
pytest scripts/tests/test_uw_scan_models.py scripts/tests/test_uw_scan_universe.py -v
```
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/uw_scan_lib/__init__.py scripts/uw_scan_lib/models.py \
        scripts/uw_scan_lib/universe.py \
        scripts/tests/test_uw_scan_models.py scripts/tests/test_uw_scan_universe.py
git commit -m "feat(uw-scan): library skeleton, models, and universe loader"
```

---

## Task 10: Signal — Deep Conviction Flow

**Files:**
- Create: `scripts/uw_scan_lib/signals/__init__.py`
- Create: `scripts/uw_scan_lib/signals/deep_conviction_flow.py`
- Test: `scripts/tests/test_uw_scan_signals_deep_conviction.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_uw_scan_signals_deep_conviction.py
from datetime import datetime
from scripts.analysis.models import TickerData
from scripts.uw_scan_lib.signals.deep_conviction_flow import detect


def _td(flow_alerts=None, earnings_within_14d=False):
    return TickerData(
        ticker="TSLA", price=200.0, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=None, rv=None, iv_percentile=None, term_structure=None,
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=flow_alerts, net_premium=None, pcr=None, darkpool=None,
        oi_changes=None, short_interest=None,
        earnings_date=None, earnings_within_14d=earnings_within_14d,
    )


def test_deep_conviction_hit_on_aggressive_ask_side_single_leg():
    alerts = [
        {
            "volume": 5000, "open_interest": 1000, "ask_side_percent": "0.85",
            "total_premium": 2_000_000, "multileg_percent": "0.05",
            "moneyness": "0.03", "expiry_dte": 21,
        },
    ]
    hit = detect("TSLA", _td(flow_alerts=alerts))
    assert hit is not None
    assert hit.tier == 1
    assert hit.signal_type == "deep_conviction_flow"


def test_no_hit_when_premium_too_low():
    alerts = [
        {
            "volume": 5000, "open_interest": 1000, "ask_side_percent": "0.85",
            "total_premium": 100_000, "multileg_percent": "0.05",
            "moneyness": "0.03", "expiry_dte": 21,
        },
    ]
    assert detect("TSLA", _td(flow_alerts=alerts)) is None


def test_no_hit_when_volume_not_exceeding_oi():
    alerts = [
        {
            "volume": 500, "open_interest": 1000, "ask_side_percent": "0.85",
            "total_premium": 2_000_000, "multileg_percent": "0.05",
            "moneyness": "0.03", "expiry_dte": 21,
        },
    ]
    assert detect("TSLA", _td(flow_alerts=alerts)) is None


def test_no_hit_when_bid_side_dominant():
    alerts = [
        {
            "volume": 5000, "open_interest": 1000, "ask_side_percent": "0.40",
            "total_premium": 2_000_000, "multileg_percent": "0.05",
            "moneyness": "0.03", "expiry_dte": 21,
        },
    ]
    assert detect("TSLA", _td(flow_alerts=alerts)) is None


def test_no_hit_when_earnings_within_2_days():
    alerts = [
        {
            "volume": 5000, "open_interest": 1000, "ask_side_percent": "0.85",
            "total_premium": 2_000_000, "multileg_percent": "0.05",
            "moneyness": "0.03", "expiry_dte": 21,
        },
    ]
    # The 2-day earnings gate is the signal's own threshold; the TickerData
    # within_14d flag is the only granularity we have, so conservative block.
    assert detect("TSLA", _td(flow_alerts=alerts, earnings_within_14d=True)) is None


def test_no_hit_when_no_flow_alerts():
    assert detect("TSLA", _td(flow_alerts=None)) is None
    assert detect("TSLA", _td(flow_alerts=[])) is None
```

- [ ] **Step 2: Run test, verify it fails**

```bash
pytest scripts/tests/test_uw_scan_signals_deep_conviction.py -v
```
Expected: module not found.

- [ ] **Step 3: Create the module**

```python
# scripts/uw_scan_lib/signals/__init__.py
"""uw-scan signal detectors. Each exports detect(ticker, td) -> SignalHit|None."""
```

```python
# scripts/uw_scan_lib/signals/deep_conviction_flow.py
"""Tier 1 signal: Deep Conviction Flow.

Detects aggressive, large, single-leg, near-the-money options orders that
suggest informed positioning.

Criteria (ALL must pass):
- volume > open_interest               (new positions, not closing)
- ask_side_percent >= 0.80             (aggressive buyer)
- total_premium >= $500K               (institutional size)
- multileg_percent < 0.10              (single-leg)
- abs(moneyness) <= 0.12               (near-the-money)
- expiry_dte >= 6                      (not 0DTE)
- no earnings within 2 days            (conservative: block if within 14d)
"""
from __future__ import annotations

from typing import Optional

from scripts.analysis.models import TickerData
from scripts.uw_scan_lib.models import SignalHit

MIN_PREMIUM = 500_000
MIN_ASK_SIDE = 0.80
MAX_MULTILEG = 0.10
MAX_MONEYNESS = 0.12
MIN_DTE = 6


def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _alert_qualifies(alert: dict) -> bool:
    vol = _f(alert.get("volume"))
    oi = _f(alert.get("open_interest"))
    ask_side = _f(alert.get("ask_side_percent"))
    premium = _f(alert.get("total_premium"))
    multileg = _f(alert.get("multileg_percent"))
    moneyness = _f(alert.get("moneyness"))
    dte = _f(alert.get("expiry_dte") or alert.get("dte"))

    if vol is None or oi is None or vol <= oi:
        return False
    if ask_side is None or ask_side < MIN_ASK_SIDE:
        return False
    if premium is None or premium < MIN_PREMIUM:
        return False
    if multileg is not None and multileg >= MAX_MULTILEG:
        return False
    if moneyness is not None and abs(moneyness) > MAX_MONEYNESS:
        return False
    if dte is None or dte < MIN_DTE:
        return False
    return True


def detect(ticker: str, td: TickerData) -> Optional[SignalHit]:
    if not td.flow_alerts:
        return None
    # Conservative earnings gate: within_14d is the only granularity; block.
    if td.earnings_within_14d:
        return None

    qualifying = [a for a in td.flow_alerts if _alert_qualifies(a)]
    if not qualifying:
        return None

    total_premium = sum(_f(a.get("total_premium")) or 0 for a in qualifying)
    top = max(qualifying, key=lambda a: _f(a.get("total_premium")) or 0)

    # Score: 0.5 base + premium-scaling (0.5 at $2M, capped)
    premium_scale = min(total_premium / 2_000_000.0, 1.0)
    score = 0.5 + 0.5 * premium_scale

    return SignalHit(
        ticker=ticker,
        signal_type="deep_conviction_flow",
        tier=1,
        score=round(score, 3),
        evidence={
            "qualifying_alerts": len(qualifying),
            "total_premium": total_premium,
            "top_strike": top.get("strike"),
            "top_expiry": top.get("expiry"),
        },
        freshness="live",
    )
```

- [ ] **Step 4: Run test, verify it passes**

```bash
pytest scripts/tests/test_uw_scan_signals_deep_conviction.py -v
```
Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/uw_scan_lib/signals/__init__.py \
        scripts/uw_scan_lib/signals/deep_conviction_flow.py \
        scripts/tests/test_uw_scan_signals_deep_conviction.py
git commit -m "feat(uw-scan): deep conviction flow tier-1 signal"
```

---

## Task 11: Signal — GEX Pinning

**Files:**
- Create: `scripts/uw_scan_lib/signals/gex_pinning.py`
- Test: `scripts/tests/test_uw_scan_signals_gex_pinning.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_uw_scan_signals_gex_pinning.py
from datetime import date, datetime
from scripts.analysis.models import TickerData
from scripts.uw_scan_lib.signals.gex_pinning import detect, MEGA_CAPS


def _td(ticker, gex_by_strike=None, price=100.0):
    return TickerData(
        ticker=ticker, price=price, fetched_at=datetime.now(),
        gex=None, gex_by_strike=gex_by_strike,
        iv=None, rv=None, iv_percentile=None, term_structure=None,
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=None, net_premium=None, pcr=None, darkpool=None,
        oi_changes=None, short_interest=None,
        earnings_date=None, earnings_within_14d=False,
    )


def test_mega_caps_contains_spy_and_qqq():
    assert "SPY" in MEGA_CAPS
    assert "QQQ" in MEGA_CAPS


def test_no_hit_outside_mega_caps():
    td = _td("WULF", gex_by_strike={"strikes": [{"strike": 100, "gamma": 5.0}]})
    assert detect("WULF", td, today=date(2026, 4, 17)) is None


def test_no_hit_outside_opex_week():
    td = _td("SPY", gex_by_strike={"strikes": [{"strike": 450, "gamma": 10.0}]}, price=450.5)
    # April 2026: 3rd Friday is April 17
    assert detect("SPY", td, today=date(2026, 4, 1)) is None


def test_hit_in_opex_week_with_near_wall():
    td = _td("SPY", gex_by_strike={"strikes": [
        {"strike": 450, "gamma": 10.0},
        {"strike": 455, "gamma": 0.1},
    ]}, price=450.5)
    hit = detect("SPY", td, today=date(2026, 4, 17))
    assert hit is not None
    assert hit.signal_type == "gex_pinning"


def test_no_hit_when_gex_by_strike_missing():
    td = _td("SPY", gex_by_strike=None)
    assert detect("SPY", td, today=date(2026, 4, 17)) is None
```

- [ ] **Step 2: Run test, verify it fails**

```bash
pytest scripts/tests/test_uw_scan_signals_gex_pinning.py -v
```
Expected: module not found.

- [ ] **Step 3: Create the module**

```python
# scripts/uw_scan_lib/signals/gex_pinning.py
"""Tier 1 signal: GEX Pinning.

Detects dealer hedging magnetic/repulsive effects at key strikes during
opex week. Only runs for mega-caps (SPY, QQQ, IWM, large individual names)
where dealer hedging is reliable.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from scripts.analysis.gex import detect_pinning, is_opex_week
from scripts.analysis.models import TickerData
from scripts.uw_scan_lib.models import SignalHit

MEGA_CAPS: frozenset[str] = frozenset({
    "SPY", "QQQ", "IWM", "DIA",
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA",
})

MIN_GAMMA = 1.0


def detect(ticker: str, td: TickerData, *, today: Optional[date] = None) -> Optional[SignalHit]:
    if ticker.upper() not in MEGA_CAPS:
        return None
    if td.gex_by_strike is None or td.price is None:
        return None

    check_date = today or date.today()
    if not is_opex_week(check_date):
        return None

    strikes = td.gex_by_strike.get("strikes") if isinstance(td.gex_by_strike, dict) else None
    if not strikes:
        return None

    pin = detect_pinning(strikes, price=td.price, opex_week=True, min_gamma=MIN_GAMMA)
    if pin is None:
        return None

    # Score: closer to price + larger gamma = higher
    distance_score = max(0.0, 1.0 - pin["distance_pct"])
    gamma_score = min(1.0, abs(pin["gamma"]) / 10.0)
    score = 0.5 * distance_score + 0.5 * gamma_score

    return SignalHit(
        ticker=ticker.upper(),
        signal_type="gex_pinning",
        tier=1,
        score=round(score, 3),
        evidence=pin,
        freshness="live",
    )
```

- [ ] **Step 4: Run test, verify it passes**

```bash
pytest scripts/tests/test_uw_scan_signals_gex_pinning.py -v
```
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/uw_scan_lib/signals/gex_pinning.py \
        scripts/tests/test_uw_scan_signals_gex_pinning.py
git commit -m "feat(uw-scan): GEX pinning tier-1 signal (mega-caps only)"
```

---

## Task 12: Signal — Earnings IV Crush

**Files:**
- Create: `scripts/uw_scan_lib/signals/earnings_iv_crush.py`
- Test: `scripts/tests/test_uw_scan_signals_earnings_iv_crush.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_uw_scan_signals_earnings_iv_crush.py
from datetime import datetime, date, timedelta
from scripts.analysis.models import TickerData
from scripts.uw_scan_lib.signals.earnings_iv_crush import detect


def _td(iv_pct, earnings_within_14d, earnings_date=None):
    return TickerData(
        ticker="X", price=100.0, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=None, rv=None, iv_percentile=iv_pct,
        term_structure=None, rr_skew_25d=None, vrp_history=None,
        flow_alerts=None, net_premium=None, pcr=None, darkpool=None,
        oi_changes=None, short_interest=None,
        earnings_date=earnings_date, earnings_within_14d=earnings_within_14d,
    )


def test_hit_on_high_iv_rank_and_earnings_in_window():
    td = _td(iv_pct=80.0, earnings_within_14d=True, earnings_date=date.today() + timedelta(days=7))
    hit = detect("AAPL", td)
    assert hit is not None
    assert hit.tier == 1


def test_no_hit_when_iv_rank_too_low():
    td = _td(iv_pct=50.0, earnings_within_14d=True, earnings_date=date.today() + timedelta(days=7))
    assert detect("AAPL", td) is None


def test_no_hit_when_earnings_not_in_window():
    td = _td(iv_pct=85.0, earnings_within_14d=False)
    assert detect("AAPL", td) is None


def test_no_hit_when_iv_rank_missing():
    td = _td(iv_pct=None, earnings_within_14d=True, earnings_date=date.today() + timedelta(days=5))
    assert detect("AAPL", td) is None


def test_no_hit_when_earnings_date_unknown_but_conservative_flag_true():
    """Conservative default earnings_within_14d=True from missing data must NOT fire."""
    td = _td(iv_pct=85.0, earnings_within_14d=True, earnings_date=None)
    assert detect("AAPL", td) is None
```

- [ ] **Step 2: Run test, verify it fails**

```bash
pytest scripts/tests/test_uw_scan_signals_earnings_iv_crush.py -v
```
Expected: module not found.

- [ ] **Step 3: Create the module**

```python
# scripts/uw_scan_lib/signals/earnings_iv_crush.py
"""Tier 1 signal: Earnings IV Crush.

Detects overpriced earnings premium — IV rank > 75 with earnings within 14 days.
This is the earnings premium-seller setup (iron condor at implied move width).
"""
from __future__ import annotations

from typing import Optional

from scripts.analysis.models import TickerData
from scripts.uw_scan_lib.models import SignalHit

MIN_IV_PCTL = 75.0


def detect(ticker: str, td: TickerData) -> Optional[SignalHit]:
    if td.iv_percentile is None or td.iv_percentile < MIN_IV_PCTL:
        return None
    # Require a KNOWN earnings date within the window. The fetcher defaults
    # earnings_within_14d=True when the date is unknown (conservative for
    # blocking signals), so we must NOT fire on that default.
    if td.earnings_date is None:
        return None
    if not td.earnings_within_14d:
        return None

    # Score: higher IV rank = stronger signal
    score = min(1.0, (td.iv_percentile - MIN_IV_PCTL) / 25.0 + 0.5)

    return SignalHit(
        ticker=ticker.upper(),
        signal_type="earnings_iv_crush",
        tier=1,
        score=round(score, 3),
        evidence={
            "iv_percentile": td.iv_percentile,
            "earnings_date": str(td.earnings_date) if td.earnings_date else "within_14d",
        },
        freshness="live",
    )
```

- [ ] **Step 4: Run test, verify it passes**

```bash
pytest scripts/tests/test_uw_scan_signals_earnings_iv_crush.py -v
```
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/uw_scan_lib/signals/earnings_iv_crush.py \
        scripts/tests/test_uw_scan_signals_earnings_iv_crush.py
git commit -m "feat(uw-scan): earnings IV crush tier-1 signal"
```

---

## Task 13: Signal — Dark Pool Accumulation + PCR context

**Files:**
- Create: `scripts/uw_scan_lib/signals/dark_pool_accumulation.py`
- Create: `scripts/uw_scan_lib/context/__init__.py`
- Create: `scripts/uw_scan_lib/context/pcr_sentiment.py`
- Test: `scripts/tests/test_uw_scan_signals_dark_pool.py`
- Test: `scripts/tests/test_uw_scan_context_pcr.py`

- [ ] **Step 1: Write the failing tests**

```python
# scripts/tests/test_uw_scan_signals_dark_pool.py
from datetime import datetime
from scripts.analysis.models import TickerData
from scripts.uw_scan_lib.signals.dark_pool_accumulation import detect


def _td(darkpool):
    return TickerData(
        ticker="X", price=100.0, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=None, rv=None, iv_percentile=None, term_structure=None,
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=None, net_premium=None, pcr=None, darkpool=darkpool,
        oi_changes=None, short_interest=None,
        earnings_date=None, earnings_within_14d=False,
    )


def test_hit_on_three_large_prints_at_similar_level():
    dp = {"data": [
        {"price": 100.0, "premium": 1_500_000},
        {"price": 100.3, "premium": 2_000_000},
        {"price": 100.2, "premium": 1_200_000},
    ]}
    hit = detect("TSLA", _td(dp))
    assert hit is not None
    assert hit.tier == 2
    assert hit.evidence["direction_neutral"] is True


def test_no_hit_with_only_two_large_prints():
    dp = {"data": [
        {"price": 100.0, "premium": 1_500_000},
        {"price": 100.3, "premium": 2_000_000},
    ]}
    assert detect("TSLA", _td(dp)) is None


def test_no_hit_with_spread_out_prices():
    dp = {"data": [
        {"price": 100.0, "premium": 1_500_000},
        {"price": 105.0, "premium": 2_000_000},
        {"price": 110.0, "premium": 1_200_000},
    ]}
    assert detect("TSLA", _td(dp)) is None


def test_no_hit_when_darkpool_missing():
    assert detect("TSLA", _td(None)) is None
```

```python
# scripts/tests/test_uw_scan_context_pcr.py
from datetime import datetime
from scripts.analysis.models import TickerData
from scripts.uw_scan_lib.context.pcr_sentiment import flag


def _td(pcr, earnings_within_14d=False):
    return TickerData(
        ticker="X", price=100.0, fetched_at=datetime.now(),
        gex=None, gex_by_strike=None,
        iv=None, rv=None, iv_percentile=None, term_structure=None,
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=None, net_premium=None, pcr=pcr, darkpool=None,
        oi_changes=None, short_interest=None,
        earnings_date=None, earnings_within_14d=earnings_within_14d,
    )


def test_pcr_extreme_fear_flag():
    f = flag("X", _td(pcr=1.6))
    assert f is not None
    assert f.label == "Extreme Fear"


def test_pcr_complacent_flag():
    f = flag("X", _td(pcr=0.3))
    assert f is not None
    assert f.label == "Complacent"


def test_pcr_neutral_returns_none():
    assert flag("X", _td(pcr=1.0)) is None


def test_pcr_skipped_when_earnings_imminent():
    assert flag("X", _td(pcr=1.6, earnings_within_14d=True)) is None


def test_pcr_skipped_when_pcr_missing():
    assert flag("X", _td(pcr=None)) is None
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
pytest scripts/tests/test_uw_scan_signals_dark_pool.py \
       scripts/tests/test_uw_scan_context_pcr.py -v
```
Expected: module not found.

- [ ] **Step 3: Create the modules**

```python
# scripts/uw_scan_lib/signals/dark_pool_accumulation.py
"""Tier 2 signal: Dark Pool Accumulation (confirmation-only).

Detects repeated large (>$1M) dark pool prints at similar price levels over
the last 5 trading days. This signal is direction-neutral and does NOT
affect raw ranking — it only contributes to confluence scoring.
"""
from __future__ import annotations

from typing import Optional

from scripts.analysis.models import TickerData
from scripts.uw_scan_lib.models import SignalHit

MIN_PRINTS = 3
MIN_PRINT_PREMIUM = 1_000_000
MAX_PRICE_SPREAD_PCT = 0.5  # prints within ±0.5% of each other count as "same level"


def detect(ticker: str, td: TickerData) -> Optional[SignalHit]:
    if not td.darkpool:
        return None
    prints = td.darkpool.get("data") if isinstance(td.darkpool, dict) else None
    if not isinstance(prints, list):
        return None

    large = [p for p in prints if float(p.get("premium") or 0) >= MIN_PRINT_PREMIUM]
    if len(large) < MIN_PRINTS:
        return None

    # Group by price level: find any cluster of >= MIN_PRINTS within MAX_PRICE_SPREAD_PCT
    for anchor in large:
        anchor_price = float(anchor.get("price") or 0)
        if anchor_price <= 0:
            continue
        cluster = [
            p for p in large
            if abs(float(p.get("price") or 0) - anchor_price) / anchor_price * 100.0 <= MAX_PRICE_SPREAD_PCT
        ]
        if len(cluster) >= MIN_PRINTS:
            total_premium = sum(float(p.get("premium") or 0) for p in cluster)
            return SignalHit(
                ticker=ticker.upper(),
                signal_type="dark_pool_accumulation",
                tier=2,
                score=min(1.0, total_premium / 10_000_000.0),
                evidence={
                    "cluster_size": len(cluster),
                    "anchor_price": anchor_price,
                    "total_premium": total_premium,
                    "direction_neutral": True,
                },
                freshness="stale",  # dark pool is T+1
            )
    return None
```

```python
# scripts/uw_scan_lib/context/__init__.py
"""uw-scan context layers — flag-only, zero numeric weight in ranking."""
```

```python
# scripts/uw_scan_lib/context/pcr_sentiment.py
"""PCR sentiment context flag (zero weight in ranking)."""
from __future__ import annotations

from typing import Optional

from scripts.analysis.models import TickerData
from scripts.uw_scan_lib.models import ContextFlag


def flag(ticker: str, td: TickerData) -> Optional[ContextFlag]:
    if td.pcr is None:
        return None
    # Earnings gate: skip if earnings imminent
    if td.earnings_within_14d:
        return None

    pcr = td.pcr
    if pcr > 1.5:
        label = "Extreme Fear"
    elif pcr > 1.2:
        label = "Elevated Fear"
    elif pcr < 0.5:
        label = "Complacent"
    else:
        return None  # neutral

    return ContextFlag(
        ticker=ticker.upper(),
        layer="pcr_sentiment",
        label=label,
        value=pcr,
    )
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
pytest scripts/tests/test_uw_scan_signals_dark_pool.py \
       scripts/tests/test_uw_scan_context_pcr.py -v
```
Expected: all 9 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/uw_scan_lib/signals/dark_pool_accumulation.py \
        scripts/uw_scan_lib/context/__init__.py \
        scripts/uw_scan_lib/context/pcr_sentiment.py \
        scripts/tests/test_uw_scan_signals_dark_pool.py \
        scripts/tests/test_uw_scan_context_pcr.py
git commit -m "feat(uw-scan): dark pool accumulation + PCR sentiment context"
```

---

## Task 14: Confluence + ranking

**Files:**
- Create: `scripts/uw_scan_lib/confluence.py`
- Create: `scripts/uw_scan_lib/ranking.py`
- Test: `scripts/tests/test_uw_scan_confluence.py`
- Test: `scripts/tests/test_uw_scan_ranking.py`

- [ ] **Step 1: Write the failing tests**

```python
# scripts/tests/test_uw_scan_confluence.py
from scripts.uw_scan_lib.models import SignalHit
from scripts.uw_scan_lib.confluence import compute_confluence, is_type_f


def _hit(sig_type, tier, score=0.8):
    return SignalHit(ticker="X", signal_type=sig_type, tier=tier, score=score, evidence={})


def test_type_f_requires_two_independent_hits():
    hits = [_hit("deep_conviction_flow", 1), _hit("gex_pinning", 1)]
    assert is_type_f(hits) is True


def test_same_signal_type_twice_is_not_type_f():
    hits = [_hit("deep_conviction_flow", 1), _hit("deep_conviction_flow", 1)]
    assert is_type_f(hits) is False


def test_dark_pool_alone_is_not_type_f():
    hits = [_hit("dark_pool_accumulation", 2)]
    assert is_type_f(hits) is False


def test_dark_pool_plus_tier1_is_NOT_type_f():
    """Dark pool is confirmation-only — it doesn't count toward independence."""
    hits = [_hit("deep_conviction_flow", 1), _hit("dark_pool_accumulation", 2)]
    assert is_type_f(hits) is False


def test_type_f_requires_two_non_darkpool_signals():
    hits = [
        _hit("deep_conviction_flow", 1),
        _hit("gex_pinning", 1),
        _hit("dark_pool_accumulation", 2),
    ]
    assert is_type_f(hits) is True


def test_confluence_score_includes_dark_pool():
    """Dark pool contributes to confluence_score even though it's excluded from Type F."""
    hits = [_hit("deep_conviction_flow", 1), _hit("dark_pool_accumulation", 2)]
    # tier1 weight 3.0 + tier2 weight 1.5 = 4.5
    assert compute_confluence(hits) == 4.5
```

```python
# scripts/tests/test_uw_scan_ranking.py
from scripts.uw_scan_lib.models import SignalHit, ContextFlag, ScanCandidate
from scripts.uw_scan_lib.ranking import build_candidate, rank_candidates, RAW_RANKING_EXCLUDE


def _hit(ticker, sig_type, tier, score=0.8):
    return SignalHit(ticker=ticker, signal_type=sig_type, tier=tier, score=score, evidence={})


def _ctx(ticker, label="Elevated Fear", value=1.3):
    return ContextFlag(ticker=ticker, layer="pcr_sentiment", label=label, value=value)


def test_dark_pool_excluded_from_raw_score():
    assert "dark_pool_accumulation" in RAW_RANKING_EXCLUDE


def test_build_candidate_raw_score_excludes_dark_pool():
    hits = [
        _hit("T", "deep_conviction_flow", 1, score=1.0),
        _hit("T", "dark_pool_accumulation", 2, score=1.0),
    ]
    c = build_candidate("T", hits, [])
    assert c is not None
    # tier1 weight 3.0 * 1.0 = 3.0; dark pool excluded from raw; confluence includes both
    assert c.raw_score == 3.0
    assert c.confluence_score == 4.5
    assert c.is_type_f is False  # dark pool doesn't count toward Type F
    assert c.final_score == 7.5


def test_build_candidate_returns_none_when_only_dark_pool():
    """Dark-pool-only hits must not produce a candidate at all."""
    hits = [_hit("T", "dark_pool_accumulation", 2, score=1.0)]
    c = build_candidate("T", hits, [])
    assert c is None


def test_context_flags_do_not_affect_final_score():
    hits = [_hit("T", "gex_pinning", 1, score=1.0)]
    c_no_ctx = build_candidate("T", hits, [])
    c_with_ctx = build_candidate("T", hits, [_ctx("T")])
    assert c_no_ctx.final_score == c_with_ctx.final_score


def test_rank_type_f_first_then_by_final_score():
    # A: no Type F, final_score 100
    # B: Type F, final_score 5
    # C: Type F, final_score 10
    # Expected order: C, B, A  (Type F primary)
    a = ScanCandidate("A", [], [], 100.0, 0.0, 100.0, is_type_f=False)
    b = ScanCandidate("B", [], [], 5.0, 0.0, 5.0, is_type_f=True)
    c = ScanCandidate("C", [], [], 10.0, 0.0, 10.0, is_type_f=True)
    ranked = rank_candidates([a, b, c])
    assert [r.ticker for r in ranked] == ["C", "B", "A"]


def test_rank_ticker_asc_tiebreak():
    x = ScanCandidate("XYZ", [], [], 10.0, 0.0, 10.0, is_type_f=False)
    a = ScanCandidate("ABC", [], [], 10.0, 0.0, 10.0, is_type_f=False)
    ranked = rank_candidates([x, a])
    assert [r.ticker for r in ranked] == ["ABC", "XYZ"]
```

- [ ] **Step 2: Run tests, verify they fail**

```bash
pytest scripts/tests/test_uw_scan_confluence.py scripts/tests/test_uw_scan_ranking.py -v
```
Expected: module not found.

- [ ] **Step 3: Create the modules**

```python
# scripts/uw_scan_lib/confluence.py
"""Type F (multi-signal confluence) detection.

Independence rule: two hits count as independent iff they have different
signal_type names. Dark Pool Accumulation contributes to confluence but is
excluded from raw ranking.
"""
from __future__ import annotations

from scripts.uw_scan_lib.models import SignalHit

CONFLUENCE_WEIGHTS = {1: 3.0, 2: 1.5}


def is_type_f(hits: list[SignalHit]) -> bool:
    """True if there are ≥2 independent non-dark-pool signal types.

    Dark Pool Accumulation is confirmation-only: it contributes to
    confluence_score but does NOT count toward Type F independence.
    """
    non_dp_types = {
        h.signal_type for h in hits
        if h.signal_type != "dark_pool_accumulation"
    }
    return len(non_dp_types) >= 2


def compute_confluence(hits: list[SignalHit]) -> float:
    """Sum tier weights across all hits (including dark pool)."""
    return sum(CONFLUENCE_WEIGHTS.get(h.tier, 0.0) for h in hits)
```

```python
# scripts/uw_scan_lib/ranking.py
"""Candidate construction and deterministic ranking.

- Dark Pool Accumulation is excluded from raw ranking (confirmation-only).
- Context flags have zero numeric weight.
- Sort order: Type F primary, final_score desc, ticker asc.
"""
from __future__ import annotations

from scripts.uw_scan_lib.confluence import CONFLUENCE_WEIGHTS, compute_confluence, is_type_f
from scripts.uw_scan_lib.models import ContextFlag, ScanCandidate, SignalHit

from typing import Optional

RANKING_TIER_WEIGHTS = {1: 3.0, 2: 1.5}
RAW_RANKING_EXCLUDE: frozenset[str] = frozenset({"dark_pool_accumulation"})


def build_candidate(
    ticker: str,
    hits: list[SignalHit],
    context_flags: list[ContextFlag],
) -> Optional[ScanCandidate]:
    """Construct a candidate from signal hits.

    Returns None if the only hit is Dark Pool Accumulation — dark pool is
    confirmation-only and does not stand alone in ranking (per spec).
    """
    non_dp_hits = [h for h in hits if h.signal_type not in RAW_RANKING_EXCLUDE]
    if not non_dp_hits:
        return None

    raw_score = sum(
        h.score * RANKING_TIER_WEIGHTS.get(h.tier, 0.0)
        for h in non_dp_hits
    )
    confluence = compute_confluence(hits)
    type_f = is_type_f(hits)
    # Context flags attach to output but do NOT contribute to final_score.
    final_score = raw_score + confluence
    return ScanCandidate(
        ticker=ticker.upper(),
        hits=hits,
        context_flags=context_flags,
        raw_score=raw_score,
        confluence_score=confluence,
        final_score=final_score,
        is_type_f=type_f,
    )


def rank_candidates(candidates: list[ScanCandidate]) -> list[ScanCandidate]:
    """Deterministic sort: Type F primary, final_score desc, ticker asc."""
    return sorted(
        candidates,
        key=lambda c: (not c.is_type_f, -c.final_score, c.ticker),
    )
```

- [ ] **Step 4: Run tests, verify they pass**

```bash
pytest scripts/tests/test_uw_scan_confluence.py scripts/tests/test_uw_scan_ranking.py -v
```
Expected: all 10 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/uw_scan_lib/confluence.py scripts/uw_scan_lib/ranking.py \
        scripts/tests/test_uw_scan_confluence.py scripts/tests/test_uw_scan_ranking.py
git commit -m "feat(uw-scan): confluence detection + deterministic ranking"
```

---

## Task 15: `uw_scan.py` — orchestrator CLI + chaining

**Files:**
- Create: `scripts/uw_scan.py`
- Test: `scripts/tests/test_uw_scan.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/tests/test_uw_scan.py
import json
from datetime import datetime
from unittest.mock import MagicMock

from scripts.analysis.models import TickerData
from scripts.uw_scan import scan_universe, ScanConfig


def _full_td(ticker, *, flow_alerts=None, darkpool=None, iv_pct=None,
             earnings_within_14d=False, gex_by_strike=None, price=100.0):
    return TickerData(
        ticker=ticker, price=price, fetched_at=datetime.now(),
        gex={"net": 1e9} if gex_by_strike else None,
        gex_by_strike=gex_by_strike,
        iv=None, rv=None, iv_percentile=iv_pct, term_structure=None,
        rr_skew_25d=None, vrp_history=None,
        flow_alerts=flow_alerts, net_premium=None, pcr=None, darkpool=darkpool,
        oi_changes=None, short_interest=None,
        earnings_date=None, earnings_within_14d=earnings_within_14d,
    )


def test_scan_targeted_mode_runs_all_signals(monkeypatch):
    # Patch fetch_ticker_data to return pre-built TickerData
    def fake_fetch(ticker, client):
        if ticker == "TSLA":
            return _full_td(
                "TSLA",
                flow_alerts=[{
                    "volume": 5000, "open_interest": 1000, "ask_side_percent": "0.85",
                    "total_premium": 2_000_000, "multileg_percent": "0.05",
                    "moneyness": "0.03", "expiry_dte": 21,
                }],
            )
        return _full_td(ticker)

    monkeypatch.setattr("scripts.uw_scan.fetch_ticker_data", fake_fetch)

    cfg = ScanConfig(mode="targeted", tickers=["TSLA", "NVDA"], full=True)
    result = scan_universe(cfg, client=MagicMock())

    assert result["mode"] == "targeted"
    assert result["universe_size"] == 2
    assert len(result["candidates"]) >= 1
    tsla = next((c for c in result["candidates"] if c["ticker"] == "TSLA"), None)
    assert tsla is not None
    assert any(h["signal"] == "deep_conviction_flow" for h in tsla["hits"])


def test_scan_output_schema_has_required_fields(monkeypatch):
    monkeypatch.setattr(
        "scripts.uw_scan.fetch_ticker_data",
        lambda t, c: _full_td(t),
    )
    cfg = ScanConfig(mode="targeted", tickers=["AAPL"], full=False)
    result = scan_universe(cfg, client=MagicMock())
    assert "scan_time" in result
    assert "regime" in result
    assert "candidates" in result


def test_scan_min_confluence_filter(monkeypatch):
    def fake_fetch(ticker, client):
        return _full_td(
            ticker,
            flow_alerts=[{
                "volume": 5000, "open_interest": 1000, "ask_side_percent": "0.85",
                "total_premium": 2_000_000, "multileg_percent": "0.05",
                "moneyness": "0.03", "expiry_dte": 21,
            }],
        )
    monkeypatch.setattr("scripts.uw_scan.fetch_ticker_data", fake_fetch)

    cfg = ScanConfig(mode="targeted", tickers=["A", "B"], full=True, min_confluence=2)
    result = scan_universe(cfg, client=MagicMock())
    # Only 1 signal each → not Type F → filtered out
    assert all(c.get("is_type_f") for c in result["candidates"])
```

- [ ] **Step 2: Run test, verify it fails**

```bash
pytest scripts/tests/test_uw_scan.py -v
```
Expected: module not found.

- [ ] **Step 3: Create `scripts/uw_scan.py`**

```python
# scripts/uw_scan.py
"""uw-scan: tiered opportunity scanner CLI.

Runs 4 signals against a ticker universe, detects multi-signal confluence,
ranks candidates deterministically, and optionally chains into
uw_analyze.run_analysis() for top-N deep dives.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Optional

from scripts.analysis.gates import earnings_gate, liquidity_gate, regime_gate
from scripts.analysis.ticker_data import fetch_ticker_data
from scripts.analysis.vrp import build_vrp_state, classify_regime
from scripts.clients.uw_client import UWClient
from scripts.uw_scan_lib.context.pcr_sentiment import flag as pcr_flag
from scripts.uw_scan_lib.models import ContextFlag, ScanCandidate, SignalHit
from scripts.uw_scan_lib.ranking import build_candidate, rank_candidates
from scripts.uw_scan_lib.signals.dark_pool_accumulation import detect as dp_detect
from scripts.uw_scan_lib.signals.deep_conviction_flow import detect as dcf_detect
from scripts.uw_scan_lib.signals.earnings_iv_crush import detect as eic_detect
from scripts.uw_scan_lib.signals.gex_pinning import detect as gp_detect
from scripts.uw_scan_lib.universe import load_universe

logger = logging.getLogger(__name__)

# v1 supports watchlist + targeted only. market_wide deferred to follow-up.
Mode = Literal["watchlist", "targeted"]


@dataclass
class ScanConfig:
    mode: Mode
    tickers: list[str] = field(default_factory=list)
    full: bool = False
    min_confluence: int = 0    # 0 = no filter; 2 = Type F only
    analyze_top: int = 0       # 0 = don't chain to analyze
    max_workers: int = 10


def _run_signals(ticker: str, td, *, full: bool, today: date) -> list[SignalHit]:
    """Run all enabled signals for this ticker. Quick mode = tier 1 only."""
    hits: list[SignalHit] = []

    # Tier 1 — always run
    for detector in (dcf_detect, eic_detect):
        hit = detector(ticker, td)
        if hit is not None:
            hits.append(hit)

    gp_hit = gp_detect(ticker, td, today=today)
    if gp_hit is not None:
        hits.append(gp_hit)

    if full:
        # Tier 2 — full mode only
        dp_hit = dp_detect(ticker, td)
        if dp_hit is not None:
            hits.append(dp_hit)

    return hits


def _run_context(ticker: str, td, *, full: bool) -> list[ContextFlag]:
    if not full:
        return []
    flags: list[ContextFlag] = []
    pcr = pcr_flag(ticker, td)
    if pcr is not None:
        flags.append(pcr)
    return flags


def scan_universe(cfg: ScanConfig, *, client: UWClient) -> dict:
    """Run a scan and return the result dict (JSON-ready).

    Parallel per-ticker fetch using ThreadPoolExecutor (10 workers by default).
    """
    # Load universe. v1 supports watchlist and targeted only; market_wide is
    # out of scope and the CLI main() rejects it before we get here.
    if cfg.mode == "targeted":
        universe = load_universe(mode="targeted", tickers=cfg.tickers)
    elif cfg.mode == "watchlist":
        universe = load_universe(mode="watchlist")
    else:
        raise ValueError(f"unsupported mode: {cfg.mode}")

    today_date = date.today()
    candidates: list[ScanCandidate] = []

    def _process(ticker: str) -> Optional[ScanCandidate]:
        try:
            td = fetch_ticker_data(ticker, client)
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch failed for %s: %s", ticker, exc)
            return None

        # Classify regime for this ticker's gates (uses its own VRP state).
        ticker_vrp = build_vrp_state(td)
        ticker_regime = classify_regime(td, ticker_vrp)

        # Apply context gates BEFORE running signals. A ticker that fails a
        # gate still produces a candidate slot (so we can surface the reason),
        # but its hits list may be empty if all signals were blocked.
        option_volume = None
        if td.flow_alerts:
            try:
                option_volume = sum(
                    int(a.get("volume") or 0) for a in td.flow_alerts
                )
            except (TypeError, ValueError):
                option_volume = None

        gates_result = {
            "earnings": "pass" if earnings_gate(
                earnings_within_14d=td.earnings_within_14d
            ) else "block",
            "liquidity": "pass" if liquidity_gate(
                option_volume=option_volume
            ) else "block",
            "regime": "pass" if regime_gate(regime=ticker_regime.regime) else "block",
        }

        # If regime gate blocks, don't run signals at all — R2 is risk-off.
        if gates_result["regime"] == "block":
            return None

        hits = _run_signals(ticker, td, full=cfg.full, today=today_date)
        flags = _run_context(ticker, td, full=cfg.full)
        if not hits:
            return None
        candidate = build_candidate(ticker, hits, flags)
        if candidate is None:
            return None
        candidate.gates = gates_result
        return candidate

    with ThreadPoolExecutor(max_workers=cfg.max_workers) as pool:
        futures = {pool.submit(_process, t): t for t in universe}
        for future in as_completed(futures):
            candidate = future.result()
            if candidate is not None:
                candidates.append(candidate)

    # Apply min_confluence filter
    if cfg.min_confluence >= 2:
        candidates = [c for c in candidates if c.is_type_f]

    ranked = rank_candidates(candidates)

    # Regime: use SPY as the market-wide proxy (lightweight fetch)
    try:
        spy_td = fetch_ticker_data("SPY", client)
        spy_vrp = build_vrp_state(spy_td)
        spy_regime = classify_regime(spy_td, spy_vrp)
        regime_dict = {"regime": spy_regime.regime, "reason": spy_regime.reason}
    except Exception as exc:  # noqa: BLE001
        logger.debug("regime fetch failed: %s", exc)
        regime_dict = {"regime": "R1", "reason": "regime probe failed"}

    result = {
        "scan_time": datetime.now().isoformat(),
        "mode": cfg.mode,
        "universe_size": len(universe),
        "candidates_analyzed": len(universe),
        "candidates_with_hits": len(candidates),
        "full": cfg.full,
        "regime": regime_dict,
        "candidates": [_candidate_to_dict(c) for c in ranked],
    }

    # Optional chain: run uw_analyze on top N
    if cfg.analyze_top > 0 and ranked:
        from scripts.uw_analyze import run_analysis
        analyses = []
        for c in ranked[: cfg.analyze_top]:
            try:
                report = run_analysis(c.ticker, fast=False, client=client)
                analyses.append(asdict(report))
            except Exception as exc:  # noqa: BLE001
                logger.warning("analyze failed for %s: %s", c.ticker, exc)
        result["analyses"] = analyses

    return result


def _candidate_to_dict(c: ScanCandidate) -> dict:
    return {
        "ticker": c.ticker,
        "is_type_f": c.is_type_f,
        "final_score": c.final_score,
        "raw_score": c.raw_score,
        "confluence_score": c.confluence_score,
        "hits": [
            {
                "signal": h.signal_type,
                "tier": h.tier,
                "score": h.score,
                "evidence": h.evidence,
                "freshness": h.freshness,
            }
            for h in c.hits
        ],
        "context_flags": [
            {"layer": f.layer, "label": f.label, "value": f.value}
            for f in c.context_flags
        ],
        "gates": c.gates,
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="uw-scan: tiered UW opportunity scanner")
    p.add_argument("tickers", nargs="*", help="Explicit ticker list (targeted mode)")
    p.add_argument("--watchlist", action="store_true", help="Scan data/watchlist.json")
    p.add_argument("--full", action="store_true", help="Full mode (tier 1 + dark pool + PCR)")
    p.add_argument("--min-confluence", type=int, default=0,
                   help="Require ≥N independent signals (2 = Type F only)")
    p.add_argument("--analyze-top", type=int, default=0,
                   help="Chain to uw_analyze for top N candidates")
    p.add_argument("--json", action="store_true", help="Print JSON to stdout")
    args = p.parse_args(argv)

    if args.watchlist:
        mode = "watchlist"
    elif args.tickers:
        mode = "targeted"
    else:
        print("ERROR: specify tickers or --watchlist. Market-wide mode is "
              "deferred to a follow-up spec — use `discover` for flow-led "
              "market-wide scanning.", file=sys.stderr)
        return 2

    cfg = ScanConfig(
        mode=mode,
        tickers=[t.upper() for t in args.tickers],
        full=args.full,
        min_confluence=args.min_confluence,
        analyze_top=args.analyze_top,
    )

    out_dir = Path("data/uw_scan")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")

    with UWClient() as client:
        result = scan_universe(cfg, client=client)

    out_file = out_dir / f"{stamp}.json"
    out_file.write_text(json.dumps(result, default=str, indent=2))

    if args.json:
        print(json.dumps(result, default=str, indent=2))
    else:
        print(f"Scan complete: {result['candidates_with_hits']} candidates "
              f"(of {result['universe_size']}) — regime {result['regime']['regime']}")
        for c in result["candidates"][:10]:
            type_f = "★" if c["is_type_f"] else " "
            signals = ",".join(h["signal"] for h in c["hits"])
            print(f"  {type_f} {c['ticker']:<6} score={c['final_score']:.1f}  [{signals}]")
        print(f"\nOutput: {out_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test, verify it passes**

```bash
pytest scripts/tests/test_uw_scan.py -v
```
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/uw_scan.py scripts/tests/test_uw_scan.py
git commit -m "feat(uw-scan): orchestrator CLI with chaining to uw-analyze"
```

---

## Task 16: Full-suite test + docs + gitignore

**Files:**
- Modify: `scripts/CLAUDE.md`
- Modify: `.gitignore`
- Create: `data/analysis/.gitkeep`
- Create: `data/uw_scan/.gitkeep`

- [ ] **Step 1: Run the full test suite**

```bash
pytest scripts/tests/test_analysis_*.py scripts/tests/test_uw_scan*.py scripts/tests/test_uw_analyze.py -v
```
Expected: all tests PASS. If any fail, fix before proceeding.

- [ ] **Step 2: Check coverage**

```bash
pytest scripts/tests/test_analysis_*.py scripts/tests/test_uw_scan*.py scripts/tests/test_uw_analyze.py \
  --cov=scripts/analysis --cov=scripts/uw_scan_lib --cov=scripts/uw_analyze --cov=scripts/uw_scan \
  --cov-report=term-missing
```
Expected: ≥90% coverage across all new modules. If below, add tests for uncovered branches (typically error paths).

- [ ] **Step 3: Add to `scripts/CLAUDE.md` commands table**

Open `scripts/CLAUDE.md` and locate the commands table. Add two rows:

```markdown
| `uw-scan` | Tiered UW signal scanner with confluence detection (distinct from `scan` which is Xenon's watchlist dark-pool HTML scan) |
| `uw-analyze [TICKER]` | Per-ticker deep-dive: VRP state, regime, 4-bucket composite score (called in-process by `uw-scan --analyze-top N`) |
```

Also add a note somewhere near the commands section:

```markdown
**`uw-scan` vs `scan`:** The legacy `scan` command is Xenon's watchlist dark-pool HTML scanner (`scripts/scanner.py`). The new `uw-scan` command is a tiered multi-signal scanner with Type F confluence detection and optional chaining to `uw-analyze`. They are distinct tools with different signal models — both are supported.
```

- [ ] **Step 4: Add to `.gitignore`**

```bash
cat >> .gitignore <<'EOF'

# uw-scan + uw-analyze outputs
data/analysis/*.json
data/uw_scan/*.json
!data/analysis/.gitkeep
!data/uw_scan/.gitkeep
EOF
```

- [ ] **Step 5: Create `.gitkeep` files**

```bash
mkdir -p data/analysis data/uw_scan
touch data/analysis/.gitkeep data/uw_scan/.gitkeep
```

- [ ] **Step 6: Smoke-test the CLIs (optional — requires live UW_TOKEN)**

```bash
python3 -m scripts.uw_analyze SPY --fast
```
Expected: prints a summary block, writes `data/analysis/SPY-<date>.json`.

```bash
python3 -m scripts.uw_scan SPY QQQ --full
```
Expected: prints a candidate list (may be empty if no signals hit), writes `data/uw_scan/<timestamp>.json`.

If network is unavailable or token is missing, skip this step — it's a smoke test, not a correctness test.

- [ ] **Step 7: Commit**

```bash
git add scripts/CLAUDE.md .gitignore data/analysis/.gitkeep data/uw_scan/.gitkeep
git commit -m "docs(uw-scan): add uw-scan + uw-analyze to commands table; gitignore outputs"
```

---

## Self-review checklist (run before marking the plan complete)

- [ ] **Spec coverage:** every section of both design docs maps to a task
  - Feature A goal → Task 8 (`run_analysis()`)
  - 4-bucket scoring → Task 6
  - VRP state + regime → Task 3
  - GEX helpers → Task 4
  - Benchmark → Task 5
  - TickerData + `iv_rank` normalization → Task 2
  - Missing-data policy → covered across Tasks 2, 3, 6, 8 (silent degradation)
  - Feature B signals → Tasks 10–13 (4 signals)
  - Confluence + ranking → Task 14
  - Orchestrator CLI + chaining → Task 15
  - Docs + gitignore → Task 16
- [ ] **No placeholders:** every code block is complete and runnable
- [ ] **Type consistency:** `bias` not `recommendation` everywhere; `mode` field present; Optional fields match missing-data policy
- [ ] **Deferred items not referenced:** no OI Buildup, Short Squeeze, or IV Skew code paths
- [ ] **Existing code untouched:** no edits to `evaluate.py`, `discover.py`, `scanner.py`, `leap_scanner_uw.py`, `web/`, `scripts/api/`
- [ ] **Four Gates:** no `TradeIdea`, no naked_short_guard integration, no Kelly sizing — v1 is analysis-only
- [ ] **Commit hygiene:** every task ends with a commit; no `--no-verify`; Co-Authored-By trailer NOT included (per user's global CLAUDE.md)

---

## Known v1 limitations (documented in `scripts/CLAUDE.md` as part of Task 16)

These are deliberate scope cuts from the tribunal-reviewed design. Each has a concrete feasibility blocker and is deferred to a named follow-up spec.

| Feature | v1 state | Reason | Follow-up |
|---|---|---|---|
| Market-wide scan mode | Not supported | Would duplicate `discover.py`'s flow-led universe logic | Separate spec that composes uw-scan tiered signals with discover's universe aggregation |
| OI Buildup signal | Not implemented | Requires 3–5 days of persisted OI snapshots that Xenon does not store | `data/oi_history/` persistence spec |
| Short Squeeze signal | Not implemented | Requires borrow-trend history (not wrapped) | Same follow-up as above |
| IV Skew context layer | Not implemented | Real-time `risk_reversal_skew` endpoint is unverified on Xenon's UW plan | Endpoint verification + wrapper spec |
| Positioning bucket | Always skipped (reweighted out) | OI history required | Same follow-up as OI Buildup |
| Flow bucket scoring | Alert count + PCR only | Net-premium + dark-pool conviction scoring deferred | Trade-generation spec (alongside Four Gates work) |
| Trade idea emission | Not supported | Gate policy questions unresolved (credit spreads, flow-edge requirement); Python naked-short guard doesn't exist | Trade-generation spec |

## Done criteria

- All 16 tasks complete with tests green
- `pytest scripts/tests/test_analysis_*.py scripts/tests/test_uw_scan*.py scripts/tests/test_uw_analyze.py` exits 0
- Coverage ≥90% on new modules
- `scripts/CLAUDE.md` documents both new commands
- `.gitignore` excludes output directories
- Git log shows one commit per task (17 commits total including Task 0)
