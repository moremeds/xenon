# Trend Scanner — Design Spec

**Date:** 2026-04-10
**Status:** Draft
**Command:** `trend-scan` (CLI) / Scanner page (web)

## Objective

Build a 3-stage pre-market trend scanner that identifies strong-trending stocks with supportive options structure for 2-week swing trades. Replaces the existing scanner page. Runs on schedule (8:30 AM ET weekdays) and on-demand from the UI.

Core thesis: **strong chart + supportive gamma structure + acceptable IV + repeated confirming flow**. Not: big premium print = buy.

## Architecture

### Approach: Shared Foundation + New Scanner

Extract common scanner primitives into `scripts/scanner_lib/` (shared by `uw_scan` and `trend_scan`). Each scanner owns its own signal detectors, scoring weights, and pipeline logic.

```
scripts/
├── scanner_lib/                    # Shared foundation
│   ├── __init__.py
│   ├── models.py                   # BaseScanCandidate, SignalHit, ContextFlag
│   ├── universe.py                 # UniverseLoader — watchlist, static index, UW, IB sources
│   ├── executor.py                 # ParallelFetcher — ThreadPoolExecutor wrapper
│   ├── cache.py                    # JSONCacheWriter — read/write data/*.json with staleness
│   └── scoring.py                  # BaseScoringModel — weighted composite with min thresholds
│
├── trend_scan_lib/                 # Trend scanner-specific
│   ├── __init__.py
│   ├── models.py                   # TrendCandidate (extends BaseScanCandidate)
│   ├── config.py                   # TrendScanConfig — weights, thresholds, universe sources
│   ├── stages/
│   │   ├── __init__.py
│   │   ├── ta_prefilter.py         # Stage A: TA trend scoring (35%)
│   │   ├── options_structure.py    # Stage B: Dealer/gamma structure scoring (25%)
│   │   ├── volatility.py           # Stage B addon: IV state scoring (20%)
│   │   └── flow_confirmation.py    # Stage C: Flow confirmation scoring (20%)
│   ├── universe.py                 # TrendUniverseBuilder — union of UW + static + IB
│   ├── ranking.py                  # TrendRanker — composite score + min gate enforcement
│   └── storage.py                  # DuckDB writer
│
├── trend_scan.py                   # Entry point — CLI + importable scan_trends()
│
├── uw_scan_lib/                    # Refactored — imports from scanner_lib where shared
└── uw_scan.py                      # Unchanged interface, updated internals
```

## Universe Building (Triple-Source Union)

Three sources fetched in parallel, then deduplicated:

### Source 1 — Static Index Constituents

- Files: `data/universe/sp500.json` + `data/universe/nasdaq100.json`
- ~600 unique tickers, refreshed manually or via periodic script
- Always available — the reliable baseline

### Source 2 — UW Screener / Flow-Active

- `UWClient.get_flow_alerts()` with filters: min premium $100k, last 5 trading days
- Extract unique tickers from flow — names with recent institutional activity
- Also pull UW market-wide dark pool data for additional flow-active tickers
- Adds ~50-200 tickers depending on market activity

### Source 3 — IB Scanner

- IB market scanner API: "Top % Gainers", "Most Active by Dollar Volume", "Hot by Price"
- Union of 2-3 scanner types
- Graceful degradation if IB Gateway is down (skip, log warning)

### Union + Dedup + Pre-filter

1. Merge all three lists → deduplicate by ticker
2. Hard floor filters:
   - Market cap > $1B
   - Average daily dollar volume > $10M
   - Price > $5
3. Expected universe: ~500-800 tickers per run

**Failure handling:** Each source is independent. If UW or IB is down, scanner runs with remaining sources. Minimum viable universe = static index list alone.

## Stage A — TA Trend Scoring (35% weight)

Runs on all universe tickers. Single OHLCV API call per ticker, parallelized with 15 workers.

**Data source:** `UWClient.get_stock_state()`. Fallback: Yahoo Finance.

### Indicators

| Indicator                | Signal                            | Scoring Logic                                   |
| ------------------------ | --------------------------------- | ----------------------------------------------- |
| MA Alignment             | `close > 20DMA > 50DMA > 200DMA`  | Full stack = 1.0, partial = 0.5, inverted = 0   |
| 20DMA Slope              | Positive slope over 5 days        | Normalized 0-1, steeper = higher                |
| RSI(14)                  | Constructive range 50-70          | Peak score at 58-65, tapers outside             |
| ADX(14)                  | Trend strength > 20               | 0-1 normalized, >25 = strong, >40 = very strong |
| MACD                     | Signal line crossover + histogram | Above signal + positive histogram = 1.0         |
| Bollinger Band Width     | Squeeze detection                 | Narrow BBW = pending breakout (bonus)           |
| Relative Strength vs SPY | Outperformance over 20 days       | RS ratio > 1.0 scores, higher = better          |
| Volume Profile           | Recent volume vs 20-day avg       | Above-average volume on up days = confirmation  |
| ATR %                    | Volatility sizing context         | Not scored — passed through for position sizing |

### Bullish Prefilter (hard gate)

- `close > 20DMA`
- Average daily dollar volume > $10M
- RSI > 40

### Bearish Prefilter (separate gate)

- `close < 20DMA`
- RSI < 60
- Negative 20DMA slope

### Breakout Detection Bonus (+0.1)

- Price within 3% of 52-week high, OR
- Price breaking above consolidation range (20-day range < 10% ATR, current close > range high)

**Output:** `trend_score` 0.0-1.0 per ticker. ~100-200 survivors pass to Stage B.

**Performance target:** <30 seconds for 800 tickers.

## Stage B — Options Structure (25%) + Volatility State (20%)

Runs on ~100-200 Stage A survivors. Heavier API calls acceptable.

### Options Structure Score (25%)

**Data sources:** `UWClient.get_greek_exposure()`, `get_greek_exposure_by_strike()`, `get_option_contracts()`, OI change.

| Component           | Bullish Signal                   | Scoring                             |
| ------------------- | -------------------------------- | ----------------------------------- |
| Spot vs Gamma Flip  | Spot above flip level            | Above = 1.0, at = 0.5, below = 0.2  |
| Net GEX Profile     | Positive / supportive gamma      | Positive = high, negative = low     |
| Call Wall Distance  | Room to run before overhead      | >5% away = 1.0, <2% = 0.3           |
| Put Wall Proximity  | Nearby support below             | Within 3% = support floor bonus     |
| Max Pain vs Spot    | Spot above max pain              | Above = favorable, pinned = penalty |
| OI Change Direction | Rising call OI on higher strikes | Net bullish OI change = 1.0         |

**Reject conditions (hard fail):**

- Severe pinning: spot within 0.5% of max pain AND high GEX concentration at spot strike
- Large overhead wall within 2% of spot with no supportive structure

### Volatility Score (20%)

**Data sources:** `UWClient.get_iv_rank()`, `get_volatility_term_structure()`, `get_realized_volatility()`

| Component            | Signal                             | Scoring                                        |
| -------------------- | ---------------------------------- | ---------------------------------------------- |
| IV Rank              | Low-moderate = cheap options       | <30 = 1.0, 30-50 = 0.7, 50-75 = 0.4, >75 = 0.2 |
| Term Structure Shape | Normal (upward sloping)            | Normal = 1.0, flat = 0.6, inverted = 0.3       |
| IV vs RV             | IV near or below RV                | IV/RV < 1.0 = options cheap (bonus)            |
| Event Premium        | Earnings/event inflating front-end | Flag, not reject — adjusts trade type          |

### Trade Type Suggestion (derived from vol score)

- IV rank < 30 + normal term structure → **debit calls/puts**
- IV rank 30-60 + structure caps move → **vertical spreads**
- IV rank > 60 + containment structure → **premium selling**

Attached to each candidate as recommendation, not a gate.

## Stage C — Flow Confirmation (20%) + Final Ranking

Runs on ~50-100 tickers that passed Stages A+B.

### Flow Confirmation Score (20%)

**Data sources:** `UWClient.get_flow_alerts(ticker=...)`, `get_greek_flow()`, dark pool data.

| Component             | Bullish Signal                      | Scoring                                            |
| --------------------- | ----------------------------------- | -------------------------------------------------- |
| Ask-side Dominance    | >60% of call flow on ask side       | >80% = 1.0, >60% = 0.7, <50% = 0.2                 |
| Flow Repetition       | Multiple trades, not single print   | ≥3 prints = 1.0, 2 = 0.6, 1 = 0.2                  |
| Expiry Clustering     | Flow in 1-4 week expiries           | Clustered = 1.0, scattered = 0.4                   |
| Strike Reasonableness | Not absurdly OTM                    | Within 10% of spot = 1.0, >15% OTM = 0.2           |
| Delta/Vega Flow       | Net positive directional greek flow | Positive = 1.0, neutral = 0.5, contradictory = 0.1 |
| Dark Pool Alignment   | DP prints align with direction      | Bullish DP cluster = bonus +0.15                   |

### News Sanity Check (flag, not scored)

- Pull headlines for ticker
- Flag if earnings within 7 days, FDA event, or major catalyst

### Final Ranking

**Composite score:**

```
final_score = (trend × 0.35) + (structure × 0.25) + (vol × 0.20) + (flow × 0.20)
```

**Minimum threshold gates (must pass ALL):**

- `trend_score ≥ 0.4`
- `structure_score ≥ 0.3`
- Liquidity floor enforced in universe build

**Top 25 candidates returned.** Both bullish and bearish candidates ranked together by `final_score`. Direction field distinguishes them. Bearish candidates use mirrored scoring (e.g., spot below gamma flip = 1.0 for bearish structure).

## Output Schema

```json
{
  "scan_id": "trend_20260410_0845",
  "scan_timestamp": "2026-04-10T08:45:12-04:00",
  "market_context": {
    "spy_close": 523.45,
    "vix_close": 18.2,
    "regime": "bullish"
  },
  "universe_size": 743,
  "stage_a_survivors": 187,
  "stage_b_survivors": 92,
  "candidates": [
    {
      "ticker": "NVDA",
      "snapshot_timestamp": "2026-04-10T08:45:12-04:00",
      "spot_price": 148.3,
      "direction": "bullish",
      "final_score": 0.82,
      "scores": {
        "trend": 0.91,
        "structure": 0.75,
        "volatility": 0.68,
        "flow": 0.85
      },
      "indicators": {
        "ma_20": 142.5,
        "ma_50": 138.2,
        "ma_200": 125.8,
        "rsi": 62.3,
        "adx": 32.1,
        "macd_histogram": 1.45,
        "bbw": 0.08,
        "rs_vs_spy": 1.15,
        "iv_rank": 22,
        "gamma_flip": 145.0,
        "call_wall": 160.0,
        "put_wall": 140.0
      },
      "summaries": {
        "trend": "Full MA stack, ADX 32, RS 1.15 vs SPY, breakout flag",
        "structure": "Spot 2.3% above gamma flip, call wall at +8%, put support at -3%",
        "vol": "IV rank 22, normal term structure, IV/RV 0.94",
        "flow": "4 ask-side call prints in 3 days, clustered 2-week expiry"
      },
      "suggested_trade": "debit_call",
      "invalidation": 142.5,
      "flags": ["earnings_in_12_days"],
      "holding_window": "5-15 trading days"
    }
  ]
}
```

## Storage — DuckDB

**Database:** `data/trend_scan.duckdb`

```sql
CREATE TABLE scan_runs (
  scan_id        VARCHAR PRIMARY KEY,
  scan_timestamp TIMESTAMPTZ NOT NULL,
  universe_size  INTEGER,
  stage_a_pass   INTEGER,
  stage_b_pass   INTEGER,
  candidates_out INTEGER,
  spy_close      DOUBLE,
  vix_close      DOUBLE,
  regime         VARCHAR,
  duration_secs  DOUBLE
);

CREATE TABLE scan_candidates (
  scan_id            VARCHAR NOT NULL REFERENCES scan_runs(scan_id),
  ticker             VARCHAR NOT NULL,
  snapshot_timestamp  TIMESTAMPTZ NOT NULL,
  spot_price         DOUBLE,
  direction          VARCHAR,
  final_score        DOUBLE,
  trend_score        DOUBLE,
  structure_score    DOUBLE,
  vol_score          DOUBLE,
  flow_score         DOUBLE,
  ma_20              DOUBLE,
  ma_50              DOUBLE,
  ma_200             DOUBLE,
  rsi                DOUBLE,
  adx                DOUBLE,
  macd_histogram     DOUBLE,
  bbw                DOUBLE,
  rs_vs_spy          DOUBLE,
  iv_rank            DOUBLE,
  gamma_flip         DOUBLE,
  call_wall          DOUBLE,
  put_wall           DOUBLE,
  suggested_trade    VARCHAR,
  invalidation       DOUBLE,
  flags              VARCHAR[],
  trend_summary      VARCHAR,
  structure_summary  VARCHAR,
  vol_summary        VARCHAR,
  flow_summary       VARCHAR,
  PRIMARY KEY (scan_id, ticker)
);
```

**Write path:** After ranking, insert into both tables. Also write `data/trend_scan.json` for the web frontend (latest scan only).

**Backtesting:** DuckDB enables SQL queries against historical scans — correlate scan scores with subsequent price movement.

## Web Integration

### Backend (FastAPI)

- New route: `POST /trend-scan` → runs `trend_scan.py --top 25` as subprocess
- Writes `data/trend_scan.json` + DuckDB
- Existing `POST /scan` kept temporarily for rollback

### API Route (Next.js)

- `web/app/api/scanner/route.ts`:
  - `GET` → reads `data/trend_scan.json`, cache_meta with 600s staleness
  - `POST` → calls `xenonFetch("/trend-scan", ...)`, same fallback pattern

### Frontend

- Replace `ScannerSections` component in `WorkspaceSections.tsx`
- Score breakdown bars per ticker (trend/structure/vol/flow)
- Direction badge (bullish/bearish)
- Suggested trade chip
- Flags (earnings, breakout)
- Sortable columns (final_score, trend_score, structure_score, etc)
- Expandable row for summaries + raw indicators
- Same `useScanner` hook pattern, updated data type

### Scheduled Pre-Market Run

- 8:30 AM ET daily, weekdays
- Same pattern as CRI scan service

## Testing Strategy

**TDD enforced:** Every implementation step follows red → green → refactor.

### Unit Tests (pytest)

| Layer                    | Test File                                                                                              | Coverage                                                                       |
| ------------------------ | ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------ |
| `scanner_lib/`           | `test_scanner_lib_models.py`, `test_scanner_lib_universe.py`, `test_scanner_lib_scoring.py`            | Base models, universe dedup, composite scoring math                            |
| `trend_scan_lib/stages/` | `test_ta_prefilter.py`, `test_options_structure.py`, `test_volatility.py`, `test_flow_confirmation.py` | Each scorer with mocked data — thresholds, gates, scoring curves               |
| `trend_scan_lib/`        | `test_trend_ranking.py`, `test_trend_universe.py`                                                      | Min threshold gates, sort order, triple-source union                           |
| `trend_scan.py`          | `test_trend_scan.py`                                                                                   | E2E pipeline with mocked clients — funnel counts, output schema, DuckDB writes |
| Frontend                 | `test_trend_scanner.test.ts`                                                                           | Component rendering, sorting, expandable rows                                  |

### Key Test Scenarios

- Ticker passes Stage A but fails Stage B pinning reject → eliminated
- All universe sources fail except static → scanner still runs
- DuckDB write failure → scanner still outputs JSON (graceful degradation)
- Breakout bonus correctly adds +0.1 only when conditions met
- Min threshold gates: ticker with 0.95 flow but 0.35 trend → rejected

### DuckDB Tests

- Use in-memory DuckDB (`:memory:`) — no mocking, tests real SQL

**Coverage target:** 95% on `scanner_lib/` and `trend_scan_lib/`.

## Dependencies

- `duckdb` Python package (pip install, ~20MB, file-based, no server)
- Existing: `UWClient`, `IBClient`, `python-dotenv`
