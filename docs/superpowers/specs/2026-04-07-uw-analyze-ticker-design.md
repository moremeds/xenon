# Feature A — `analyze` Ticker Analysis

**Date:** 2026-04-07
**Status:** Draft v3 — second revision after tribunal round 2
**Related:** Feature B — `uw-scan` Opportunity Scan (separate doc, **primary v1 deliverable**)
**Primary entry point:** `uw-scan --analyze-top N` (Feature B chains into this code)
**Secondary entry point:** `uw-analyze TICKER` CLI — debug/research escape hatch only, not the intended operator workflow

## Goal

Provide a reusable **per-ticker analysis library** (`run_analysis(ticker) -> AnalysisReport`) that produces a scored, read-only options signal summary with VRP state, regime classification, benchmark context, and a 4-bucket composite. Feature B's `uw-scan --analyze-top N` calls this in-process; a thin `uw-analyze` CLI exposes it for debugging/research.

**Positioning:** This is the "deep-dive subroutine" of the `uw-scan` workflow, not a standalone operator tool. It does not replace `evaluate.py`'s 7-milestone pipeline and does not emit trade recommendations. Its output is a **signal summary** (inputs to the operator's decision), not a BUY/SELL recommendation.

**Scope change from v1 draft (tribunal revision):** Feature A v1 is **analysis-only**. It does NOT emit `TradeIdea` objects. Trade idea generation is deferred to a follow-up spec after (a) a Python-native naked-short guard exists and (b) Gate 1/Gate 2 policy questions around credit spreads and flow-edge requirements are resolved with the user. This avoids silently weakening the Four Gates.

This is the Xenon-native port of the **analytical layers** from the `unusual-whales` skill's single-ticker workflow, stripped of scraping, delivery, and trade emission.

## Non-goals

- **No trade idea emission in v1** — output is a scored analysis report, not a trade recommendation. A later spec will add trade generation after the guard + gate policy work.
- No UI / order-builder changes (`web/`, `scripts/api/` untouched)
- No auto-order placement
- No Playwright, Chrome profile, Discord/email delivery
- No modifications to `evaluate.py`, `discover.py`, `leap_scanner_uw.py`
- No outcome tracking / calibration (deferred)
- No durable workflow orchestration — batch command, not long-lived
- No scenario analysis / IV smile / payoff diagram (frontend concern)
- **No `R3` regime** — the "rebound from selloff" state requires time-series inputs v1 does not fetch. Enum is `R0 | R1 | R2` only.

## User-facing contract

```bash
uw-analyze TSLA                         # single ticker, pretty-printed summary + JSON file
uw-analyze TSLA --json                  # JSON to stdout (for scripting)
uw-analyze TSLA --fast                  # skip flow + positioning buckets (GEX + Vol only)
uw-analyze TSLA NVDA AAPL               # multi-ticker (sequential, one JSON per ticker)
```

The CLI is named `uw-analyze` (not `analyze`) to avoid any future collision with a broader "analyze" command and to match the `uw-scan` / `uw-analyze` naming pair.

Default output: human-readable summary to stdout + machine-readable JSON written to `data/analysis/{TICKER}-{YYYYMMDD}.json`.

The command exits `0` on success (including analysis with missing buckets). Non-zero only on infrastructure failures (network, auth, missing config).

### Public Python API (consumed by Feature B for chaining)

```python
# scripts/uw_analyze.py
def run_analysis(
    ticker: str,
    *,
    fast: bool = False,
    client: UWClient | None = None,
) -> AnalysisReport:
    """Run the full analysis pipeline. CLI wraps this function.

    Feature B's `uw-scan --analyze-top N` calls this directly in-process.
    If `client` is None, a fresh UWClient is created and closed before return.
    """
```

This is the stable cross-feature contract. Changing its signature requires updating Feature B in lock-step.

## Architecture

### Module layout

```
scripts/
  uw_analyze.py                       # CLI entry point + run_analysis() public API
  analysis/                           # NEW — pure-function analysis library
    __init__.py
    vrp.py                            # VRP raw, z-score, regime classifier (R0/R1/R2 only)
    gex.py                            # flip point, walls, pinning detection
    scoring.py                        # 4-bucket composite score + grade
    gates.py                          # earnings, liquidity, regime gates (context gates — NOT the Four Gates)
    benchmark.py                      # SPY/QQQ/sector ETF context loader
    ticker_data.py                    # TickerData dataclass + fetch_ticker_data() aggregator
    models.py                         # @dataclass types: VRPState, RegimeState, BucketScores, AnalysisReport
  tests/
    test_analyze.py                   # CLI + run_analysis() end-to-end with mocked client
    test_analysis_vrp.py              # VRP math, regime classifier (R0/R1/R2 truth table)
    test_analysis_gex.py              # flip detection, wall ranking, pinning
    test_analysis_scoring.py          # bucket weights, composite, grade mapping, reweighting, fast-mode
    test_analysis_gates.py            # earnings/liquidity/regime gates in isolation
    test_analysis_benchmark.py        # SPY + sector ETF loader with mocked responses
    test_analysis_ticker_data.py      # TickerData aggregator and missing-data handling
```

`scripts/analysis/` is a shared library. Feature B (`uw-scan`) reuses `vrp.py`, `gex.py`, `gates.py`, `benchmark.py`, and `ticker_data.py` unchanged.

**Not in this module** (deferred to future trade-generation spec): `strikes.py`, `naked_short.py`, trade idea dataclasses, Kelly integration.

### Data flow

```
analyze.run_analysis(ticker)
    │
    ├─▶ ticker_data.fetch_ticker_data(ticker, client) → TickerData
    │       ├─ get_greek_exposure               → GEX by strike, net gamma
    │       ├─ get_greek_exposure_by_strike     → walls + flip
    │       ├─ get_volatility_stats             → iv, rv, iv_rank (raw 0-1; normalized on ingest)
    │       ├─ get_volatility_term_structure    → per-expiry IVs
    │       ├─ get_variance_risk_premium ⚠      → VRP history (endpoint existence unverified — see Risks §1)
    │       ├─ get_flow_alerts                  → net premium, P/C ratio
    │       ├─ get_oi_changes                   → OI bias (current only — no history)
    │       ├─ get_historical_risk_reversal_skew → 25Δ skew snapshot (T+1; the only RR skew wrapper that exists)
    │       ├─ get_short_data ⚠                 → SI, utilization, DTC (existing wrapper at uw_client.py:557)
    │       ├─ darkpool (existing fetch_flow path) → dark pool conviction
    │       ├─ get_earnings_by_ticker           → next earnings date (existing wrapper at uw_client.py:690)
    │       └─ benchmark.load_benchmark_context(client) → SPY + sector ETF GEX/IV
    │
    ├─▶ vrp.build_vrp_state(ticker_data)        (pure function)
    │       └─ VRPState { vrp_raw, vrp_zscore?, iv_percentile, ts_ratio, ts_inverted,
    │                     earnings_within_14d, data_freshness }
    │
    ├─▶ vrp.classify_regime(ticker_data, vrp_state)  (pure function)
    │       └─ RegimeState { regime ∈ {R0,R1,R2}, reason, gex_sign, gex_flip_relative, flip_distance_pct }
    │
    ├─▶ scoring.score_buckets(ticker_data, vrp_state, regime, fast=False)  (pure function)
    │       └─ BucketScores { market_structure, volatility, flow, positioning,
    │                          composite, grade, recommendation, reweighted, skipped_buckets }
    │
    └─▶ AnalysisReport { ticker, price, benchmark, vrp, regime, scores, notes, data_freshness }
        → write data/analysis/{TICKER}-{YYYYMMDD}.json + print summary
```

All functions in `scripts/analysis/*` take dicts/dataclasses in, return dataclasses out. No I/O — the only I/O is in `ticker_data.fetch_ticker_data()` and `benchmark.load_benchmark_context()`.

### Four Gates — not enforced in v1 (analysis-only)

Feature A v1 produces an **analysis report**, not trade recommendations. It does NOT create `TradeIdea` objects and does NOT exercise any of the Four Gates. The composite score and VRP state are inputs to a human operator (or a later trade-generation spec), not auto-generated orders.

Rationale (from tribunal review):
- Gate 1 (Convexity ≥ 2:1) has no credit-spread carve-out in root CLAUDE.md — any VRP put credit spread would fail it
- Gate 2 (Edge) requires a specific flow/dark-pool signal — the skill's 4-bucket composite does not guarantee this
- Gate 4 (No naked shorts) requires a Python-native guard that does not currently exist (`web/lib/nakedShortGuard.ts` is TypeScript; `scripts/naked_short_audit.py` audits existing orders, not hypothetical ideas)

Resolving these is prerequisite to emitting trades, and is properly its own design doc. Feature A stays narrowly scoped to what it can ship **today without weakening Xenon's gate policy**.

### Data contract: `TickerData`

Single dataclass with one field per input the scoring pipeline needs. Every bucket must declare its dependencies here so missing-data handling is explicit.

```python
@dataclass(frozen=True)
class TickerData:
    ticker: str
    price: Optional[float]                 # None if GEX/price lookup failed
    fetched_at: datetime
    # Market Structure bucket
    gex: Optional[dict]                    # get_greek_exposure
    gex_by_strike: Optional[dict]          # walls + flip
    # Volatility bucket
    iv: Optional[float]                    # volatility/stats → 0-100 (normalized)
    rv: Optional[float]                    # volatility/stats → 0-100 (normalized)
    iv_percentile: Optional[float]         # iv_rank * 100 — ALWAYS 0-100 in TickerData
    term_structure: Optional[list[dict]]   # per-expiry IV
    rr_skew_25d: Optional[float]           # 25Δ skew magnitude
    vrp_history: Optional[list[float]]     # trailing 252d VRP (None if endpoint unavailable)
    # Flow bucket
    flow_alerts: Optional[list[dict]]
    net_premium: Optional[dict]
    pcr: Optional[float]
    darkpool: Optional[dict]
    # Positioning bucket
    oi_changes: Optional[list[dict]]
    short_interest: Optional[dict]         # SI%, utilization, DTC
    # Context
    earnings_date: Optional[date]
    earnings_within_14d: bool              # always set; defaults True if date unknown (conservative)

    def bucket_available(self, bucket: Literal["market_structure","volatility","flow","positioning"]) -> bool:
        ...
```

Each bucket's `bucket_available()` rule:
- `market_structure`: `gex` AND `gex_by_strike` present
- `volatility`: `iv` AND `iv_percentile` AND `term_structure` present
- `flow`: `flow_alerts` OR `net_premium` present (either suffices)
- `positioning`: `oi_changes` present (short_interest is bonus, not required)

If `bucket_available()` returns False, `scoring.score_buckets` sets the bucket to 0 and marks it in `skipped_buckets`.

### `iv_rank` normalization rule

**Single canonical rule:** UW's `iv_rank` field is a float `0..1`. Immediately after fetching volatility stats, `ticker_data.py` multiplies by 100 and stores as `TickerData.iv_percentile` (range `0..100`). **All downstream code uses `iv_percentile` and never touches raw `iv_rank`.**

All thresholds in this design (`>30`, `>60`, `>75`) refer to `iv_percentile` and use the 0..100 scale.

Test: `test_analysis_ticker_data.py` includes a fixture with raw `{"iv_rank": "0.65", "iv": "0.28", "rv": "0.21"}` and asserts `TickerData.iv_percentile == 65.0`.

## Data model

```python
# scripts/analysis/models.py
@dataclass(frozen=True)
class VRPState:
    vrp_raw: Optional[float]          # iv - rv (0-100 scale); None if iv or rv missing
    vrp_zscore: Optional[float]       # None if history unavailable — NO proxy fallback
    iv_percentile: Optional[float]    # 0-100 (normalized); None if vol stats missing
    ts_ratio: Optional[float]         # near_iv / far_iv; None if term_structure missing
    ts_inverted: Optional[bool]       # ts_ratio > 1.05; None if ts_ratio is None
    earnings_within_14d: bool         # conservative default True if earnings_date unknown
    data_freshness: Literal["live", "stale", "unavailable"]

@dataclass(frozen=True)
class RegimeState:
    regime: Literal["R0", "R1", "R2"]      # R3 dropped — requires time series v1 does not fetch
    reason: str                             # human-readable derivation
    gex_sign: Optional[Literal["positive", "negative", "mixed"]]  # None if GEX unavailable
    gex_flip_relative: Optional[Literal["above_price", "below_price", "at_price"]]
    flip_distance_pct: Optional[float]     # None if GEX unavailable or price unknown

@dataclass(frozen=True)
class BenchmarkContext:
    spy: BenchmarkSnapshot
    sector_etf: Optional[BenchmarkSnapshot]

@dataclass(frozen=True)
class BenchmarkSnapshot:
    ticker: str
    iv_rank: float
    gex_regime: Literal["positive", "negative", "mixed"]
    gex_flip: float
    price: float
    data_date: str
    freshness: Literal["live", "stale", "unavailable"]

@dataclass(frozen=True)
class BucketScores:
    market_structure: float           # ±28
    volatility: float                 # ±28
    flow: float                       # ±24
    positioning: float                # ±20
    composite: float                  # ±100, re-weighted if a bucket is unavailable
    grade: Literal["A", "B", "C"]
    bias: Literal["STRONGLY_BULLISH", "BULLISH", "MIXED", "BEARISH", "STRONGLY_BEARISH"]
    # ^ 'bias' NOT 'recommendation' — this is a signal summary, not a trade recommendation.
    #   Operators must not auto-trade on this field.
    mode: Literal["full", "fast"]
    reweighted: bool
    skipped_buckets: list[str]

@dataclass(frozen=True)
class AnalysisReport:
    ticker: str
    price: Optional[float]            # None if price lookup failed
    fetched_at: str
    data_freshness: dict[str, str]    # per-section freshness map (see Missing-data policy)
    benchmark: BenchmarkContext
    vrp: VRPState
    regime: RegimeState
    scores: BucketScores
    notes: list[str]                  # setup-specific observations (not trade ideas)
    # NOTE: No trade_ideas field in v1 — this is analysis-only
```

`TradeIdea` and its gate-enforcement machinery are deliberately omitted from v1. A follow-up spec will introduce them alongside the Python-native naked-short guard and the Gate 1/Gate 2 policy clarifications.

All dataclasses are frozen and JSON-serializable via a shared `to_dict()` helper.

## VRP computation

1. `vrp_raw = iv - rv` from `volatility/stats` (both already normalized to 0-100 in `TickerData`)
2. `vrp_zscore`:
   - **Primary:** from `variance_risk_premium` endpoint, trailing ~252 days: `(vrp_today - mean(history)) / max(std(history), 0.01)`
   - **Fallback:** **None.** If the endpoint is unavailable, `vrp_zscore = None` and the VRP-dependent bits of the regime classifier degrade gracefully (regime falls back to GEX+TS signals only). **No `vrp_rank` proxy** — that field does not exist in `volatility/stats`.
   - **Future (separate spec):** a local rolling history of `iv - rv` snapshots, written daily by a monitor-daemon handler, can replace the missing endpoint once we have ≥60 trading days of snapshots.
3. `ts_ratio = near_iv / far_iv` from `volatility_term_structure`. `near_iv` = first expiry with DTE > 7; `far_iv` = expiry closest to 90 DTE. If `term_structure` has fewer than 2 usable expiries, `ts_ratio = None`.
4. **Regime classification (R0/R1/R2 only):**

   ```
   if ts_inverted == True and (vrp_zscore is not None and vrp_zscore < 0):
       → R2, "Term structure inverted + VRP negative"
   elif net_gex < 0 and flip_distance_pct > 2.0 and (vrp_zscore is None or vrp_zscore < 0.3):
       → R2, "Deeply negative GEX + thin/unknown VRP"
   elif ts_inverted == True or (vrp_zscore is not None and vrp_zscore < 0.3):
       → R1, f"Caution: {inverted TS or thin VRP}"
   elif gex_flip_relative == "below_price" and vrp_zscore is not None and vrp_zscore > 0.5:
       → R0, "Positive GEX + elevated VRP"
   else:
       → R1, "Mixed signals"
   ```

   `vrp_zscore is None` is treated as "unknown" and biases classification toward R1 (cautious), never toward R0.

## 4-bucket scoring

Weights lifted from the skill with no changes for v1. These are tunable constants in `scoring.py`, not magic numbers scattered through the code.

| Bucket | Signals | Max | Source |
|---|---|---|---|
| Market Structure | GEX flip vs live price, walls, DEX concentration, vanna+charm bias | ±28 | `greek_exposure` |
| Volatility | IV rank, IV-HV spread, skew direction, term structure shape | ±28 | `volatility_stats`, `volatility_term_structure` |
| Flow | Net premium, call/put ratio, dark pool conviction | ±24 | existing `fetch_flow` + `flow_alerts` |
| Positioning | OI change bias, short interest (σ-relative), squeeze risk | ±20 | existing `fetch_oi_changes` |

**Score → bias mapping (signal summary, not a trade recommendation):**
- +60 to +100 → STRONGLY_BULLISH
- +20 to +59 → BULLISH
- −19 to +19 → MIXED
- −20 to −59 → BEARISH
- −60 to −100 → STRONGLY_BEARISH

**Important:** These labels are informational signal summaries. They are NOT trade recommendations. An operator must never auto-trade on `bias` alone — trade decisions require structure design, Kelly sizing, and Four Gates enforcement, none of which this feature performs.

**Bucket failure handling:** If a bucket is unavailable via `TickerData.bucket_available()`, its score is 0 and the remaining buckets are re-weighted to maintain the ±100 scale. Formula:

```
available_max = sum(max_weight for bucket in buckets if available)
composite = sum(score for available buckets) * (100 / available_max)
```

`BucketScores.reweighted = True` and `skipped_buckets = [...]` make this visible.

**`--fast` mode** (operator intent, not data failure):
- Runs only Market Structure + Volatility buckets
- Uses the same re-weighting formula: `available_max = 28 + 28 = 56`, composite = `(ms + vol) * (100/56)`
- `skipped_buckets = ["flow", "positioning"]`, `reweighted = True`
- Grade is capped at B regardless of computed grade

**Worked example (fast mode):**
```
market_structure = +20, volatility = -14
composite = (20 + -14) * (100/56) = 6 * 1.786 = +10.7
grade = min(computed_grade, B)   # capped at B in fast mode (cannot exceed B)
bias = MIXED (composite in [-19, +19])
```

**Worked example (data failure — Flow bucket missing mid-run):**
```
market_structure = +20, volatility = -14, flow = MISSING, positioning = +10
available_max = 28 + 28 + 20 = 76
composite = (20 + -14 + 10) * (100/76) = 16 * 1.316 = +21.1
grade = computed grade (no fast-mode cap)
bias = BULLISH (composite in [+20, +59])
skipped_buckets = ["flow"]
```

Distinction: **fast-mode skip** and **data-failure skip** use the same formula but are tracked separately in `BucketScores` so callers can tell them apart (`BucketScores.mode = "full" | "fast"`).

## Trade idea generation — DEFERRED

Trade idea generation is **not part of Feature A v1**. The directional trade table, VRP put-credit-spread entry conditions, strike picker, and spread builder from the source skill will live in a follow-up spec that also delivers:
- A Python-native naked-short guard (`scripts/analysis/naked_short.py::check_trade_idea(legs, positions)`)
- Explicit resolution of Gate 1 for credit spreads (amend root policy OR block credit spreads entirely)
- Explicit resolution of Gate 2 for composite-score-only trades (require flow-edge prerequisite OR drop directional auto-emission)
- Kelly integration against `data/portfolio.json`

Until that spec ships, operators consume the `AnalysisReport` and hand-build structures in the existing order UI (same workflow as `evaluate`'s M5 handoff).

## Missing-data policy

**Single table, applies across both Feature A and Feature B.**

| Data | On 404 / empty / stale | `data_freshness` flag | Downstream effect |
|---|---|---|---|
| GEX (`greek_exposure`) | Fail bucket | `unavailable` | `market_structure` bucket = 0, re-weighted; Regime falls back to VRP-only inputs (usually R1) |
| Volatility stats | Fail VRP entirely | `unavailable` | `vrp_state.vrp_raw = None`, `iv_percentile = None`; Volatility bucket dropped |
| Term structure | `ts_ratio = None`, `ts_inverted = None` | `stale` | Regime cannot use TS inversion rule — still classifies from GEX + VRP |
| VRP endpoint | `vrp_zscore = None` | `stale` | Regime biases toward R1; no VRP-based analysis notes |
| Earnings calendar | `earnings_within_14d = True` (conservative) | `stale` | Any earnings-sensitive notes flagged "assuming earnings imminent" |
| Dark pool | Flow bucket fed with flow_alerts only | `stale` | Flow bucket scored on reduced inputs |
| Short interest | `short_interest = None` | `stale` | Positioning bucket uses OI only (still scored, not dropped) |
| Benchmark (SPY or sector) | `BenchmarkContext.sector_etf = None` | `stale` | SPY-only comparison; cross-ticker notes skipped |
| Ticker not valid / no options | Hard exit | N/A | Non-zero exit code, mirrors `evaluate.run_evaluation` M1 failure |

**Policy:** Data-driven degradation is **silent and non-blocking** — the analysis still runs. The report's `data_freshness` dict makes the degradation visible to the operator. No "fail open to a trade" is possible because v1 does not emit trades.

## Testing strategy

**Unit tests (pytest, ~95% coverage target):**
- `test_analysis_vrp.py` — VRP raw arithmetic, z-score with mocked 252-day history, **z-score None when history unavailable**, regime classifier R0/R1/R2 truth table including `vrp_zscore is None` cases
- `test_analysis_gex.py` — flip detection with synthetic GEX curves, wall ranking, pinning detection on opex week
- `test_analysis_scoring.py` — each bucket in isolation; full-mode composite; data-failure reweighting; fast-mode reweighting (with worked example fixture); grade cap in fast mode; `mode` field distinguishes the two skip paths
- `test_analysis_gates.py` — earnings/liquidity/regime gates in isolation
- `test_analysis_benchmark.py` — SPY + sector ETF loader, sector fallback mapping, sector-missing path
- `test_analysis_ticker_data.py` — `iv_rank` normalization (raw 0.65 → `iv_percentile == 65.0`), `bucket_available()` rules, missing-data defaults

**Integration tests:**
- `test_analyze.py` — `run_analysis()` end-to-end with a fully mocked `UWClient`; asserts JSON output schema, `data_freshness` flags, and `AnalysisReport` field invariants
- One golden fixture per regime (R0 / R1 / R2) to pin end-to-end behavior
- One "VRP endpoint missing" fixture asserting graceful degradation
- One "all buckets fail except Market Structure" fixture asserting the reweighting formula

**No browser tests** — this is a pure backend command with no UI.

## Files touched / created

**Created:**
- `scripts/analyze.py` (~200 lines: argparse, `run_analysis()`, orchestration, output formatting)
- `scripts/analysis/__init__.py`
- `scripts/analysis/vrp.py` (~150 lines)
- `scripts/analysis/gex.py` (~200 lines)
- `scripts/analysis/scoring.py` (~250 lines)
- `scripts/analysis/gates.py` (~80 lines — context gates only; NO naked-short guard)
- `scripts/analysis/benchmark.py` (~120 lines)
- `scripts/analysis/ticker_data.py` (~200 lines — aggregator + bucket_available + normalization)
- `scripts/analysis/models.py` (~120 lines of dataclasses)
- `scripts/tests/test_analyze.py`
- `scripts/tests/test_analysis_{vrp,gex,scoring,gates,benchmark,ticker_data}.py`
- `data/analysis/` directory (gitignored except .keep)

**Modified:**
- `scripts/clients/uw_client.py` — add endpoint wrappers as needed (see Risks §1 for verification steps):
  - `get_variance_risk_premium(ticker, timespan="1y")` — **only if the endpoint exists on Xenon's UW plan**; otherwise skip and document the `vrp_zscore = None` fallback
- `scripts/CLAUDE.md` — add `analyze` to the commands table
- `CLAUDE.md` — no changes (root policy is unchanged; trade gate questions are deferred)

**Not touched:**
- `scripts/evaluate.py`, `scripts/discover.py`, `scripts/leap_scanner_uw.py` — zero changes
- `web/`, `scripts/api/` — zero changes

## Risks and unknowns

1. **VRP endpoint existence.** `variance_risk_premium` is not in Xenon's current OpenAPI spec. Implementation step 1: hit `/api/volatility/variance_risk_premium/SPY?timespan=1y` with the production token. If 200 → add the wrapper and run with z-score. If 404/403 → ship with `vrp_zscore = None` and the regime classifier's null-handling path. **No `vrp_rank` fallback** — that field does not exist.
2. **Earnings calendar source.** Use the existing `UWClient.get_earnings_by_ticker(ticker)` wrapper (already present at `uw_client.py:690`). Returns historical earnings for the ticker; derive next earnings date from the series. If the endpoint returns empty or the next date is unknown, default `earnings_within_14d = True` (conservative). **No yfinance dependency** — that package is not in Xenon's requirements.
3. **Term structure shape.** `get_volatility_term_structure` is wrapped; response shape needs a 15-minute verification during implementation before finalizing `ts_ratio` math.
4. **Bucket weight calibration.** The ±28/28/24/20 weights are lifted from the skill unchanged. They are named constants in `scoring.py` for easy tuning after live data is collected.
5. **Scoring overlap with `evaluate` M4.** `evaluate.determine_edge` produces pass/fail; `analyze` produces a scored report. Different outputs, no conflict. A future spec may fold scoring into `evaluate` if calibration shows it's more predictive.
6. **Analysis-only scope is deliberately narrow.** Feature A v1 is essentially a "scored context report." The business value is that operators get a structured VRP/regime/composite view before hand-building structures — they don't today. Trade auto-generation is valuable but gate-blocked, so it waits.

## Open questions

None blocking. The design is ready for implementation planning once the user confirms the scope and module layout.

---

## Spec self-review

- **Placeholders:** None. All sections have concrete content.
- **Internal consistency:** Data flow, module layout, and testing strategy all reference the same `scripts/analysis/` layout. Feature A explicitly does not touch `evaluate`/`discover`/`leap_scanner_uw`, consistent throughout.
- **Scope:** Single focused feature (`analyze` command). No decomposition needed. Outcome tracking explicitly deferred. Feature B is a separate doc.
- **Ambiguity:** VRP endpoint fallback is called out as a risk with a concrete remediation. Earnings source has a documented fallback chain. Bucket weights are named constants, not magic numbers.
