# Feature B — `uw-scan` Opportunity Scan

**Date:** 2026-04-07
**Status:** Draft v3 — second revision, **PRIMARY v1 DELIVERABLE**
**Depends on:** Feature A — `uw-analyze` Ticker Analysis library (shared analysis code, invoked via `run_analysis()`)
**Ship order:** Feature B ships first. Feature A's shared library (`scripts/analysis/*`) is built as part of Feature B and consumed in-process via `run_analysis()`. The `uw-analyze` CLI is a thin wrapper added in the same spec but is secondary.

## Goal

Ship a standalone `uw-scan` command that screens a ticker universe (watchlist or flow-alert-based market universe) for high-conviction options setups using a tiered signal list, applies context gates (earnings, liquidity, regime), flags multi-signal confluence (Type F), and outputs a ranked candidate list. Optionally chains into `analyze` for top-N deep dives.

**Naming (tribunal revision):** The command is `uw-scan` (not `scan`) — Xenon already has a `scan` command (`scripts/scanner.py`, watchlist dark-pool HTML scan) in the existing command table. The new module lives at `scripts/uw_scan.py`.

**Scope (tribunal v2 revision — v1 shrunk):**

v1 ships **4 signals**, not 6. The following are **deferred** to a follow-up spec:
- **OI Buildup** — requires 3–5 days of historical OI per strike, which Xenon does not persist today (`fetch_oi_changes.py` returns current deltas only)
- **Short Squeeze Powder Keg** — requires borrow-trend history, not wrapped in `UWClient`
- **IV Skew context layer** — real-time `risk_reversal_skew/{T}?expiry=E` endpoint unverified; only `get_historical_risk_reversal_skew` (T+1 snapshot) is wrapped. Skew detection deferred.

v1 ships:
- **Tier 1:** Deep Conviction Flow, GEX Pinning, Earnings IV Crush
- **Tier 2:** Dark Pool Accumulation (confirmation-only, not in raw ranking)
- **Context layer:** PCR Sentiment only (flag-only, zero weight in ranking)

v1 market-wide universe is **flow-led only** — tickers from `fetch_options_flow` at the configured min-premium floor. Watchlist and targeted modes have no such constraint.

**Why this cut:** Every deferred piece has a concrete feasibility blocker (no historical persistence, unverified endpoint). Shipping the 4 signals we can actually support cleanly beats shipping 6 with two broken and one approximated.

**Positioning vs existing `discover`:** `discover.py` is the flow/dark-pool aggregator with a single 0–100 score per ticker. `uw-scan` is the tiered signal + confluence scanner. Both are candidate finders; `uw-scan` is the richer one and adds multi-signal confluence detection that `discover` lacks. Documented in `scripts/CLAUDE.md` so operators know when to use which.

This is the Xenon-native port of the `--scan --full` workflow from the `unusual-whales` skill, stripped of scraping and delivery.

## Non-goals

- No UI / order-builder changes
- No auto-order placement
- No Playwright / Chrome / Discord / email delivery
- No modifications to `evaluate.py`, `leap_scanner_uw.py`
- **`discover.py` is not replaced.** `discover` keeps its current market-wide dark-pool + flow scoring as-is. `scan` is a parallel command with a different signal model (tiered + confluence, not a single composite score).
- No durable workflow orchestration
- No outcome tracking / calibration (deferred to the same future spec as Feature A)

## User-facing contract

```bash
uw-scan                                    # market-wide flow-led, quick mode (~3-5 min)
uw-scan --full                             # market-wide flow-led, all tiers + PCR context (~8-12 min)
uw-scan --watchlist                        # run against data/watchlist.json (singular — Xenon's existing file)
uw-scan AAPL MSFT NVDA                     # targeted scan of an explicit list
uw-scan --full --analyze-top 3             # full scan, then uw_analyze.run_analysis() on top 3 in-process
uw-scan --json                             # JSON output
uw-scan --min-confluence 2                 # only return tickers with ≥2 independent signals (Type F)
```

**Mode definitions (concrete):**

| Mode | Signals run | Context layer | Runtime estimate |
|---|---|---|---|
| quick (default) | Tier 1 only: Deep Conviction Flow, GEX Pinning, Earnings IV Crush | none | ~3–5 min |
| `--full` | Tier 1 + Dark Pool Accumulation | PCR Sentiment (flag-only) | ~8–12 min |

**No A–G setup taxonomy in v1.** The output JSON emits `is_type_f: bool` and the list of hit signal names. Operators and downstream code classify from the signal list — there is no abstracted "setup_type" enum.

Default output: ranked table to stdout + JSON to `data/scan/{YYYYMMDD-HHMM}.json`. Top-N in `docs/status.md` via existing status writer.

## Architecture

### Module layout

```
scripts/
  uw_scan.py                           # CLI entry point (argparse + orchestration)
  uw_analyze.py                        # thin CLI wrapper for run_analysis() (built in the same spec)
  uw_scan_lib/                         # scan-specific logic package (renamed to avoid module/package collision)
    __init__.py
    universe.py                        # watchlist / market-wide ticker universe loader
    signals/                           # one module per signal — pure detect(ticker, data) -> SignalHit | None
      __init__.py
      deep_conviction_flow.py          # Tier 1
      gex_pinning.py                   # Tier 1
      earnings_iv_crush.py             # Tier 1
      dark_pool_accumulation.py        # Tier 2 (confirmation-only)
    context/
      pcr_sentiment.py                 # PCR flag-only (zero weight in ranking)
    confluence.py                      # Type F detection
    ranking.py                         # deterministic sort (Type F primary, final_score secondary)
    models.py                          # SignalHit, ScanCandidate, ScanResult
  analysis/                            # shared analysis library (consumed by both uw_scan and uw_analyze)
    __init__.py
    vrp.py                             # VRP raw, z-score, regime classifier (R0/R1/R2)
    gex.py                             # flip, walls, pinning detection
    scoring.py                         # 4-bucket composite + bias label
    gates.py                           # earnings, liquidity, regime gates (context gates, NOT Four Gates)
    benchmark.py                       # SPY/sector ETF context
    ticker_data.py                     # TickerData dataclass + fetcher
    models.py                          # VRPState, RegimeState, BucketScores, AnalysisReport
  tests/
    test_uw_scan.py                    # CLI + orchestration with mocked client
    test_uw_scan_signals_{deep_conviction,gex_pinning,earnings_iv_crush,dark_pool}.py
    test_uw_scan_context_pcr.py
    test_uw_scan_confluence.py
    test_uw_scan_ranking.py
    test_uw_analyze.py                 # run_analysis() + thin CLI
    test_analysis_{vrp,gex,scoring,gates,benchmark,ticker_data}.py
```

**Naming note (critical):** the CLI entry is `scripts/uw_scan.py` (module) and the package is `scripts/uw_scan_lib/` (directory). These do NOT collide in Python's import system. Same pattern for `uw_analyze.py` alongside the shared `analysis/` package.

**Omitted from v1:** `signals/oi_buildup.py`, `signals/short_squeeze.py`, `context/iv_skew.py`. Follow-up spec adds them after historical-OI persistence, borrow-trend ingestion, and real-time skew endpoint verification.

**Reused from Feature A (no duplication):**
- `scripts/analysis/vrp.py` — regime classification (R0/R1/R2) for the regime gate
- `scripts/analysis/gex.py` — flip point + walls for GEX pinning signal
- `scripts/analysis/gates.py` — earnings, liquidity gates
- `scripts/analysis/benchmark.py` — SPY market context
- `scripts/analysis/ticker_data.py` — per-ticker data fetch aggregator
- `scripts/analysis/models.py` — `RegimeState`, `BenchmarkContext`
- `scripts/analyze.run_analysis(ticker)` — called in-process by `--analyze-top N`

### Data flow

```
uw_scan.py
    │
    ├─▶ universe.load_universe(mode, watchlist, tickers) → list[str]
    │       - market-wide: existing fetch_options_flow → aggregate tickers (flow-led only, see Scope)
    │       - watchlist:   load data/watchlists/{name}.json
    │       - targeted:    caller-provided list
    │
    ├─▶ parallel per-ticker fetch via UWClient (ThreadPoolExecutor, 10 workers — matches discover.py)
    │       For each ticker, build a TickerData (reuses analysis/ticker_data.fetch_ticker_data)
    │       Required endpoints (all already wrapped in UWClient):
    │         - get_greek_exposure                  (GEX pinning, Market Structure)
    │         - get_volatility_stats                (iv_percentile, IV crush detection)
    │         - get_volatility_term_structure       (regime, near/far IV)
    │         - get_flow_alerts                     (deep conviction)
    │         - darkpool feed (existing fetch_flow path)
    │         - get_earnings_by_ticker              (earnings IV crush gate)
    │       All these wrappers exist today. NO new UWClient wrappers required for v1.
    │
    ├─▶ signals/*.detect(ticker, data) → list[SignalHit]   (one per matching signal)
    │       each hit: {ticker, signal_type, tier, score, evidence, triggered_at}
    │
    ├─▶ context/*.flag(ticker, data, cross_section) → list[ContextFlag]
    │       skew/PCR computed cross-sectionally across the batch
    │
    ├─▶ gates.py gates applied per ticker:
    │       - earnings gate (exclude signals where earnings ≤N days, N varies by signal)
    │       - liquidity gate (skip signals if total option vol < 1000)
    │       - regime gate (disable skew flagging entirely in R2)
    │
    ├─▶ confluence.detect_type_f(hits) → adds TYPE_F label when ≥2 independent tier-1 or tier-2 hits
    │
    ├─▶ ranking.rank_candidates(hits, flags) → ScanCandidate[] sorted by confluence_score desc
    │
    └─▶ write data/scan/{timestamp}.json + print summary + (optional) chain to analyze for top N
```

All signal modules are pure: `detect(ticker: str, data: TickerData) -> Optional[SignalHit]`. They do no I/O and no cross-ticker reasoning. Cross-sectional logic lives only in `context/` and `confluence.py`.

## Signal specifications

### Tier 1 — High predictive value

#### Deep Conviction Flow (`deep_conviction_flow.py`)
**Detects:** Aggressive informed positioning in options.
**Criteria (ALL must pass):**
- Volume > Open Interest (new positions)
- ≥80% filled at ask
- ≥$500K premium (configurable floor)
- Single-leg (<10% multileg ratio in the flow window)
- Near-the-money (≤12% OTM)
- ≥6 DTE
**Gates:** Exclude if earnings within 2 days.
**Output:** `SignalHit(tier=1, score = f(premium, volume/OI, aggressiveness))`

#### GEX Pinning (`gex_pinning.py`)
**Detects:** Dealer hedging magnetic/repulsive effects at key strikes.
**Criteria:**
- Current date within 3 calendar days of monthly opex (3rd Friday)
- Large gamma concentration at a strike near (±1%) current price
- `$gamma_per_1pct` above a configurable threshold
**Gates:** Only SPY, QQQ, and configured mega-caps (small/mid-caps have unreliable dealer hedging).
**Output:** `SignalHit(tier=1, score = normalized gamma density)`

#### Earnings IV Crush (`earnings_iv_crush.py`)
**Detects:** Overpriced earnings premium.
**Criteria:**
- IV rank > 75
- Earnings within 14 days
- Implied move readable from term structure
**Structure suggestion** (emitted as a note, not a trade): Iron condor at 1x implied move width, 30–45 DTE.
**Output:** `SignalHit(tier=1, score = f(iv_rank, days_to_earnings, implied_move))`

### Tier 2 — Moderate predictive value

#### OI Buildup at Strike (`oi_buildup.py`)
**Detects:** Multi-day accumulation at a specific strike.
**Criteria:**
- Same strike showing OI increases over 3–5 consecutive trading days
- Volume consistently > existing OI each day
- Majority ask-side (buying) or bid-side (selling) — classified, not filtered
**Requires:** Historical OI snapshot — Xenon already writes OI data via `fetch_oi_changes`. Signal reads from the existing `data/` cache; if <3 days of history, signal returns None with reason.
**Output:** `SignalHit(tier=2, score = f(days_sustained, premium_size, otm_distance))`

#### Short Squeeze Powder Keg (`short_squeeze.py`)
**Detects:** Preconditions for a gamma+short squeeze.
**Criteria:**
- Short Interest > 20% of float
- Utilization > 90%
- Days to Cover > 5
- Rising cost to borrow (trailing 5-day)
- Simultaneously: call OI increasing (gamma squeeze fuel)
**Gates:** Requires catalyst thesis — signal emits `requires_catalyst=True` metadata; downstream analyst/news data (from existing `fetch_analyst_ratings`) can satisfy it.
**Output:** `SignalHit(tier=2, score = f(SI, utilization, DTC, borrow_trend, call_oi_growth))`

#### Dark Pool Accumulation (`dark_pool_accumulation.py`)
**Detects:** Institutional accumulation at a price level.
**Criteria:**
- ≥3 dark pool prints >$1M at similar price levels (±0.5%) over trailing 5 days
- **Does NOT infer direction** — MM hedging makes short volume ratio unreliable
**Output:** `SignalHit(tier=2, score = total_premium, meta={"direction_neutral": True})` — **confirmation-only**, does not stand alone in ranking.

## Context layers (flag-only, not ranked)

### IV Skew (`context/iv_skew.py`)
- Cross-sectional 25Δ risk-reversal proxy across the scan batch
- Batch-relative z-score (no time series)
- Flags: `AVOID_LONGS` (z>2), `CAUTION` (z>1.5), `PREFERRED` (z<−1)
- **Earnings gate:** exclude tickers with earnings ≤10 days
- **Regime gate:** disable entirely if regime is R2
- **Liquidity gate:** skip if total option volume < 1000

### PCR Sentiment (`context/pcr_sentiment.py`)
- `pcr = total_put_volume / total_call_volume` per ticker
- Fixed thresholds: >1.5 Extreme Fear, >1.2 Elevated Fear, 0.5–1.2 Neutral, <0.5 Complacent
- **Earnings gate:** exclude if earnings ≤5 trading days
- **Liquidity gate:** skip if total option volume < 1000
- **Asymmetry:** high PCR (contrarian buy) weighted stronger than low PCR (sell)

## Confluence detection (Type F)

`confluence.detect_type_f(hits)` returns `{ticker: confluence_score}` where:
- Confluence score = sum of tier weights for all independent signal hits on the same ticker
- Tier 1 hit weight: 3
- Tier 2 hit weight: 1.5
- Context flag weight: 0 (flag-only)
- Two hits of the same signal type do NOT stack — "independent" means different signal types
- Dark Pool Accumulation is excluded from single-signal ranking but contributes to confluence

A ticker is labeled **TYPE_F (Multi-Signal Confluence)** if it has ≥2 independent tier-1/tier-2 hits (excluding dark-pool accumulation from the count, though it still contributes to `confluence_score`).

**"Independent" rule:** Two hits count as independent iff they have different `signal_type` names. Known correlated pairs to document in code: `deep_conviction_flow` + `oi_buildup` often read the same underlying strike/expiry. The confluence function does NOT deduplicate correlated pairs in v1 — this is a known limitation flagged in Risks.

## Ranking

**Dark Pool Accumulation and context layers are excluded from raw ranking.** Only signals contribute to `final_score`; context flags are output metadata only with **zero numeric weight**.

```python
RANKING_TIER_WEIGHTS = {"tier1": 3.0, "tier2": 1.5}
RAW_RANKING_EXCLUDE = {"dark_pool_accumulation"}  # dark pool is confirmation-only

raw_score = sum(
    hit.score * RANKING_TIER_WEIGHTS[f"tier{hit.tier}"]
    for hit in hits
    if hit.signal_type not in RAW_RANKING_EXCLUDE
)
final_score = confluence_score + raw_score
# context_flags are attached to the output but DO NOT affect final_score
```

**Sort order (primary key: Type F status):**

```python
candidates.sort(
    key=lambda c: (not c.is_type_f, -c.final_score, c.ticker),
)
```

Type F candidates always rank above non-Type-F, with `final_score` desc secondary and ticker asc tiebreak. Deterministic, testable, and consistent with the "context = flag-only" rule elsewhere in the spec.

`ranking.py` is a pure function — `rank_candidates(hits, flags) -> list[ScanCandidate]`.

## Output format

```jsonc
{
  "scan_time": "2026-04-07T10:30:00-04:00",
  "mode": "market-wide|watchlist|targeted",
  "universe_size": 500,
  "universe_source": "market-wide" | "watchlist:core" | "targeted:[AAPL,MSFT,NVDA]",
  "regime": { ... },                        // from analysis.vrp.classify_regime on SPY
  "candidates_analyzed": 500,
  "candidates_with_hits": 47,
  "candidates": [
    {
      "ticker": "TSLA",
      "is_type_f": true,                    // ≥2 independent signal hits (no A-G taxonomy in v1)
      "final_score": 12.5,
      "confluence_score": 9.0,
      "hits": [
        {"signal": "deep_conviction_flow", "tier": 1, "score": 0.82, "evidence": {...}},
        {"signal": "gex_pinning", "tier": 1, "score": 0.71, "evidence": {...}}
      ],
      "context_flags": [
        {"layer": "pcr_sentiment", "label": "Elevated Fear", "value": 1.35}
      ],
      "gates": {"earnings": "pass", "liquidity": "pass", "regime": "pass"}
    }
  ]
}
```

## Chaining into `uw-analyze`

`--analyze-top N` runs the full scan, then invokes `uw_analyze.run_analysis(ticker, client=shared_client)` for each of the top N tickers in-process (sharing the `UWClient` to avoid reconnecting). Results append to the scan JSON under `analyses: [...]`.

The `run_analysis()` signature is specified in Feature A's "Public Python API" section and is the stable cross-feature contract. Implementation-wise, Feature A's `scripts/analysis/*` library and `scripts/uw_analyze.py` are delivered as part of this feature's implementation plan — they are not a separate ship.

## Four Gates relationship

**Important:** `uw-scan` is a **candidate finder**, not a trade recommender. Feature A v1 is also not a trade recommender (analysis-only). Neither feature emits `TradeIdea` objects. The Four Gates will apply to the future trade-generation spec that follows both A and B.

The scan's context gates (earnings / liquidity / regime) are **signal quality gates**, not the Four Gates — they exist to prevent noise, not to block trades. A ticker can appear in scan output while being something an operator would never trade; the gating happens downstream when the operator (or a future auto-trader) turns the candidate into an order.

## Missing-data policy

Inherits the Feature A Missing-data policy table verbatim, plus:

| Data | On 404 / empty / stale | Effect |
|---|---|---|
| `risk_reversal_skew` | Fall back to `volatility_percentiles.skewness` | Label context flag `[~APPROX]`; skew still scored |
| `risk_reversal_skew` AND `volatility_percentiles` | Both unavailable | Skew context layer skipped entirely for that ticker |
| OI history <3 days | N/A | OI Buildup signal returns None with `reason="insufficient_history"` |
| `short_interest` | None | Squeeze signal returns None with `reason="short_interest_unavailable"` |
| Earnings calendar unknown | conservative | Earnings-gated signals treat earnings as imminent (block signal); direction-neutral signals (dark pool, GEX pinning) still run |

## Testing strategy

**Unit tests (~95% coverage target):**
- `test_uw_scan_signals_deep_conviction.py` — criteria in isolation (positive / boundary / gate-fail / liquidity-fail)
- `test_uw_scan_signals_gex_pinning.py` — opex-week detection, gamma concentration threshold, mega-cap allowlist
- `test_uw_scan_signals_earnings_iv_crush.py` — iv_percentile + earnings window truth table
- `test_uw_scan_signals_dark_pool.py` — direction-neutral flag, 5-day window
- `test_uw_scan_context_pcr.py` — PCR buckets, earnings gate, liquidity gate
- `test_uw_scan_confluence.py` — hit combinations, Type F labeling, independence rule
- `test_uw_scan_ranking.py` — deterministic ordering, Type F primary sort key, context_flags have zero numeric effect, dark-pool exclusion

**Integration tests:**
- `test_uw_scan.py` — full CLI with fully mocked UWClient; asserts ranking, output schema, `--analyze-top N` chaining
- Golden fixture: 10 synthetic tickers with hand-designed hits → assert exact ranking order
- VRP endpoint unavailable fixture → regime classifies as R1, scan still runs

**No browser tests** — backend command, no UI.

## Files touched / created

**Created:**
- `scripts/uw_scan.py` (~300 lines)
- `scripts/uw_scan_lib/` package (~1000 lines — 4 signals + 1 context + confluence + ranking + models)
- `scripts/uw_analyze.py` (~200 lines — CLI + `run_analysis()`)
- `scripts/analysis/` package (~900 lines — vrp, gex, scoring, gates, benchmark, ticker_data, models)
- `scripts/tests/test_uw_scan*.py`, `test_uw_analyze.py`, `test_analysis_*.py`
- `data/uw_scan/` directory (gitignored except .keep) — output files live here
- `data/analysis/` directory (gitignored except .keep) — `uw-analyze` output lives here

**Modified:**
- `scripts/CLAUDE.md` — add `uw-scan` and `uw-analyze` rows to the commands table; document `uw-scan` vs existing `scan` positioning (`scan` = Xenon watchlist dark-pool HTML; `uw-scan` = tiered UW signal scanner with confluence)
- `scripts/clients/uw_client.py` — **optional, conditional:** add `get_variance_risk_premium(ticker, timespan="1y")` ONLY if the endpoint returns 200 on a manual probe during implementation step 1. If 404/403, skip the wrapper and ship with `vrp_zscore = None`. **No other new wrappers required** — Feature B v1's signal set uses endpoints that are already wrapped.

**Not touched:**
- `scripts/evaluate.py`, `scripts/discover.py`, `scripts/scanner.py`, `scripts/leap_scanner_uw.py` — zero changes
- Existing `data/watchlist.json` — read but not written
- `web/`, `scripts/api/` — zero changes

**Not touched:**
- `scripts/analysis/*` — built in Feature A, imported here read-only
- `scripts/analyze.py` — imported by scan for chaining, not modified
- `scripts/evaluate.py`, `scripts/discover.py`, `scripts/leap_scanner_uw.py` — zero changes
- `web/`, `scripts/api/` — zero changes

## Risks and unknowns

1. **OI Buildup and Short Squeeze deferred.** Historical persistence blockers. The spec cuts them cleanly out of v1 rather than faking them. Follow-up spec adds them after `data/oi_history/{TICKER}.jsonl` snapshotting exists and borrow-trend ingestion is wrapped.
2. **IV Skew context layer deferred.** Only the T+1 historical RR skew endpoint is wrapped; the real-time endpoint is unverified. Deferred to the same follow-up.
3. **Dark pool direction-neutrality.** Codified in `RAW_RANKING_EXCLUDE`.
4. **Market-wide mode runtime.** `--full` ~8–12 min. Not a durable-execution problem (re-run is cheap). Progress flagged to stderr.
5. **Confluence double-counting.** With 3 tier-1 signals in v1 (flow, pinning, earnings crush), correlated-pair double-counting is low risk — the signals read different data paths. Revisit if future signals introduce correlated pairs.
6. **Market-wide universe is flow-led only.** Pure earnings-IV-crush candidates with no flow are only reachable via watchlist/targeted modes. Documented, not a bug.
7. **VRP endpoint availability.** `get_variance_risk_premium` may not exist on Xenon's UW plan. Implementation step 1: probe `/api/volatility/variance_risk_premium/SPY?timespan=1y`. 200 → add wrapper, use z-score. 404/403 → ship with `vrp_zscore = None` and the regime classifier's null-handling path (already specified in Feature A). No other endpoints are at risk — everything else is already wrapped.
8. **Feature A's library scope.** This spec delivers `scripts/analysis/*` and `scripts/uw_analyze.py` as part of its implementation plan, alongside `scripts/uw_scan.py` and `scripts/uw_scan_lib/*`. The plan should be structured so the shared library lands first (and is testable in isolation), then the two CLIs land on top.

## Open questions

None blocking. Ready for implementation planning once Feature A's shared library is in place.

---

## Spec self-review

- **Placeholders:** None.
- **Internal consistency:** Signal list matches the skill's tier taxonomy. Shared-library reuse from Feature A is called out explicitly (analysis/vrp, gex, gates, benchmark, models). No duplication between Features A and B.
- **Scope:** Single feature (`scan` command). Does not replace `discover` — they coexist with different signal models. Chaining to `analyze` is opt-in via flag.
- **Ambiguity:** Confluence "independence" rule is called out as a known pitfall with a documented resolution. Context gates are distinguished from the Four Gates to prevent confusion. Dark pool signal's direction-neutrality is explicit.
