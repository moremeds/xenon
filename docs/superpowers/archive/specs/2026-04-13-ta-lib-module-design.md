# TA-Lib Module Design

## Overview

Internal `scripts/ta_lib/` module that computes technical analysis indicators using [TA-Lib](https://ta-lib.org/) on IB historical data, with DuckDB as a persistent read-through cache. First consumer: `trend_scan.py`.

## Goals

1. Replace hand-rolled pandas TA math in `trend_scan.py` (lines 296–388) with TA-Lib
2. Switch OHLC data source from UW API to IB historical data
3. Persist OHLC bars and computed indicators in DuckDB (`data/ta.duckdb`)
4. Read-through cache pattern: consumers always read from DB; cache miss triggers IB fetch → TA-Lib compute → DB write
5. Design schema to support multiple timeframes (daily only in v1)

## Module Structure

```
scripts/ta_lib/
├── __init__.py
├── bars.py          # IB OHLC fetch → list[BarData] → DataFrame
├── indicators.py    # TA-Lib wrappers + post-processing
├── store.py         # DuckDB read/write
└── service.py       # Read-through cache orchestrator (single entry point)
```

### `bars.py`

Fetches OHLC bars from IB via `ib_client.get_historical_data()`.

- Duration: 1 year (~260 trading days, enough for SMA-200 + 52-week high with holiday buffer) on cold start
- Incremental: fetches bars from `last_bar_date` onward on stale cache. The last cached bar is intentionally re-fetched and UPSERTed to handle corrections. IB dates (`formatDate=1`, `"yyyyMMdd"`) are parsed to `datetime.date` before persistence.
- Bar size: `1 day` (v1)
- What to show: `TRADES`
- Returns: `pandas.DataFrame` with columns `[open, high, low, close, volume, date]`

**Contract construction:** Builds `ib_insync.Stock(ticker, "SMART", "USD")` and qualifies via `ib.qualifyContracts()` before requesting data. Invalid/ambiguous symbols raise `ValueError` with the ticker name. Empty historical responses (delisted, no data) raise `RuntimeError` — callers handle gracefully without poisoning the cache.

**IB response conversion:** `ib_client.get_historical_data()` returns `list[BarData]`, not a DataFrame. `bars.py` converts to DataFrame explicitly, parsing date strings and casting numeric columns.

### `indicators.py`

Thin wrappers around TA-Lib functions. Each function takes OHLC numpy arrays and returns indicator values.

Indicators (matching current `trend_scan.py`):

| Indicator | TA-Lib Function | Parameters                                   |
| --------- | --------------- | -------------------------------------------- |
| SMA(20)   | `talib.SMA`     | timeperiod=20                                |
| SMA(50)   | `talib.SMA`     | timeperiod=50                                |
| SMA(200)  | `talib.SMA`     | timeperiod=200                               |
| RSI(14)   | `talib.RSI`     | timeperiod=14                                |
| MACD      | `talib.MACD`    | fastperiod=12, slowperiod=26, signalperiod=9 |
| ADX(14)   | `talib.ADX`     | timeperiod=14                                |
| BBands    | `talib.BBANDS`  | timeperiod=20, nbdevup=2, nbdevdn=2          |
| ATR(14)   | `talib.ATR`     | timeperiod=14                                |

A single `compute_all(df: DataFrame) -> DataFrame` function runs all indicators and returns the input DataFrame with indicator columns appended.

**Derived columns computed after TA-Lib:**

- `bb_width = (bb_upper - bb_lower) / bb_middle` (TA-Lib only returns upper/middle/lower)
- `atr_pct = atr_14 / close` (percentage ATR, used by scoring)

**Post-processing rules (warmup & edge cases):**

- TA-Lib returns `NaN` for warmup windows — these are left as `NaN` in stored rows (only the latest row matters for scoring)
- RSI on flat series: TA-Lib returns `NaN` → coerce to `50.0`
- RSI all-up (no losses): TA-Lib returns `100.0` (matches current behavior)
- RSI all-down (no gains): TA-Lib returns `0.0` (matches current behavior)

**ADX/ATR behavioral note:** TA-Lib uses Wilder's exponential smoothing for ADX and ATR, while the current pandas code uses simple rolling averages. This is an intentional upgrade to mathematically correct implementations. Scoring thresholds in `ta_prefilter.py` should be validated against TA-Lib outputs and adjusted if needed. Regression tests should freeze new baselines from TA-Lib, not attempt to match the old pandas outputs.

### `store.py`

DuckDB storage layer at `data/ta.duckdb`.

**Schema:**

```sql
CREATE TABLE IF NOT EXISTS ohlc_bars (
    ticker     VARCHAR NOT NULL,
    timeframe  VARCHAR NOT NULL,
    bar_date   TIMESTAMPTZ NOT NULL,
    open       DOUBLE NOT NULL,
    high       DOUBLE NOT NULL,
    low        DOUBLE NOT NULL,
    close      DOUBLE NOT NULL,
    volume     BIGINT NOT NULL,
    fetched_at TIMESTAMPTZ DEFAULT current_timestamp,
    PRIMARY KEY (ticker, timeframe, bar_date)
);

CREATE TABLE IF NOT EXISTS ta_indicators (
    ticker          VARCHAR NOT NULL,
    timeframe       VARCHAR NOT NULL,
    bar_date        TIMESTAMPTZ NOT NULL,
    sma_20          DOUBLE,
    sma_50          DOUBLE,
    sma_200         DOUBLE,
    rsi_14          DOUBLE,
    macd            DOUBLE,
    macd_signal     DOUBLE,
    macd_histogram  DOUBLE,
    adx_14          DOUBLE,
    bb_upper        DOUBLE,
    bb_middle       DOUBLE,
    bb_lower        DOUBLE,
    bb_width        DOUBLE,
    atr_14          DOUBLE,
    computed_at     TIMESTAMPTZ DEFAULT current_timestamp,
    PRIMARY KEY (ticker, timeframe, bar_date)
);
```

Design decisions:

- **Separate tables** — OHLC is source-of-truth, indicators are derived. Indicator params change → recompute without re-fetching from IB.
- **`timeframe` column** — supports multi-timeframe without schema migration.
- **`TIMESTAMPTZ`** for `bar_date` — supports intraday timeframes in future (daily bars stored as midnight UTC).
- **`OHLC NOT NULL`** — reject/clean null bars from IB before persistence. If IB returns a bar with null fields, skip it and log a warning.
- **`fetched_at`** on `ohlc_bars` — staleness detection.
- **`computed_at`** on `ta_indicators` — tracks when indicators were last computed (for future recomputation triggers).
- **Indicator columns nullable** — warmup window rows legitimately have `NaN` values.

Functions:

- `init_schema(conn)` — creates tables if not exist
- `read_ohlc(conn, ticker, timeframe) -> DataFrame | None`
- `write_ohlc(conn, ticker, timeframe, df)` — UPSERT
- `read_indicators(conn, ticker, timeframe) -> DataFrame | None`
- `write_indicators(conn, ticker, timeframe, df)` — UPSERT
- `get_latest_bar_date(conn, ticker, timeframe) -> date | None` — staleness check

### `service.py`

Single entry point for consumers. Orchestrates the read-through cache.

```python
class TAService:
    def __init__(self, db_path="data/ta.duckdb", ib_client=None):
        # Opens single shared DuckDB connection, inits schema
        # ib_client: injected for testing, defaults to real IB client

    def get_indicators(self, ticker: str, timeframe: str = "1d") -> pd.DataFrame:
        # Returns full history DataFrame with OHLC + all indicator columns
        # Read-through: DB hit → return; DB miss/stale → fetch → compute → write → return

    def get_snapshot(self, ticker: str, timeframe: str = "1d") -> dict:
        # Returns latest-row dict matching the shape trend_scan.py expects
        # Calls get_indicators() internally, then extracts .iloc[-1] + derived scalars
        # Keys: ticker, close, ma_20, ma_50, ma_200, rsi, adx, macd, macd_signal,
        #        macd_histogram, bbw, atr_pct, high_52w, ma_20_series,
        #        recent_avg_volume, avg_20d_volume, recent_up_ratio,
        #        range_20d_pct, dollar_volume, price
        # Note: market_cap is NOT included (comes from stock_info, not OHLC/TA)

    def bulk_refresh(self, tickers: list[str], timeframe: str = "1d") -> None:
        # Pre-fetches OHLC for all stale tickers with IB pacing (see IB Pacing section)
        # Called once before scan starts, serialized on main thread
        # After bulk_refresh, individual get_snapshot() calls are all cache hits
```

**Field name mapping:** The DB stores TA-Lib native names (`sma_20`, `rsi_14`, `adx_14`, `bb_width`, `atr_14`). `get_snapshot()` maps these to the names `trend_scan.py` expects (`ma_20`, `rsi`, `adx`, `bbw`, `atr_pct`).

**Derived snapshot fields** (computed by `get_snapshot()` from the full DataFrame, not stored in DB):

| Field               | Source                                                       |
| ------------------- | ------------------------------------------------------------ |
| `ma_20_series`      | Last 5 values of `sma_20` column                             |
| `recent_up_ratio`   | Fraction of last 10 days with positive close-to-close change |
| `recent_avg_volume` | Mean of last 5 days' volume                                  |
| `avg_20d_volume`    | Mean of last 20 days' volume                                 |
| `high_52w`          | Max of last 252 bars' high                                   |
| `range_20d_pct`     | (max high - min low) / close over last 20 bars               |
| `atr_pct`           | `atr_14 / close` (latest bar)                                |
| `dollar_volume`     | `close * avg_20d_volume`                                     |
| `price`             | Alias for `close`                                            |
| `ticker`            | Passed through                                               |

**Note:** `market_cap` is not part of this module — it comes from `UWClient.get_stock_info()` or equivalent. `trend_scan.py` must retain that lookup separately.

**Read-through flow:**

1. Query `ta_indicators` for (ticker, timeframe)
2. If rows exist and latest `bar_date` >= last completed trading session → JOIN with `ohlc_bars`, return
   - "Last completed trading session" determined via market calendar (see Freshness section)
3. If miss or stale:
   a. Determine fetch range (full ~260 bars on cold start, incremental from last `bar_date` on stale)
   b. Fetch OHLC from IB via `bars.py`
   c. **Within a single DB transaction:**
   - UPSERT into `ohlc_bars`
   - Read full OHLC series back from DB
   - Run `indicators.compute_all()` on full series (TA-Lib needs lookback window)
   - UPSERT into `ta_indicators`
     d. Return joined result

**Freshness detection:** Uses the project's existing market calendar (`scripts/utils/market_calendar.py`) to determine the last completed US equity session. This correctly handles:

- Monday pre-market (last session = Friday)
- Market holidays (last session = prior trading day)
- Weekends (last session = Friday)
- Regular trading hours (last session = yesterday if before 16:00 ET, or today if after)

**Stock split / corporate action handling:** On each incremental fetch, compare the last cached close with the first new bar's open. If the gap exceeds 30% (configurable), assume a corporate action occurred and force a full re-fetch of the entire history, replacing all cached bars. This purges stale unadjusted prices. IB returns split-adjusted historical data, so a full re-fetch self-corrects.

## Integration with `trend_scan.py`

**Before:**

```
UW API → _bars_frame() → fetch_ohlcv() [hand-rolled TA] → ta_prefilter scoring
```

**After:**

```
TAService.bulk_refresh(universe)  # serialized IB fetches, main thread
  ↓
parallel_fetch(universe):
  TAService.get_snapshot(ticker)  # all cache hits, no IB calls
    ↓
  ta_prefilter scoring (unchanged)
```

Changes to `trend_scan.py`:

- **Remove:** `_bars_frame()`, `_build_price_frame()`, ~90 lines of pandas TA math in `fetch_ohlcv()`
- **Remove:** UW OHLC dependency (`uw_client.get_stock_ohlc()`)
- **Replace `fetch_ohlcv()`:** Calls `TAService.get_snapshot()` which returns the same dict shape
- **Add:** `TAService.bulk_refresh(universe + ["SPY"])` call before `parallel_fetch` — pre-warms cache for all tickers plus SPY benchmark
- **Keep:** All scoring functions in `ta_prefilter.py` unchanged (same column names via mapping)
- **Keep:** Relative strength vs SPY (`rs_vs_spy`) stays in `trend_scan.py` (cross-ticker logic) — SPY data comes from `TAService.get_snapshot("SPY")`
- **Keep:** `market_cap` lookup from `UWClient.get_stock_info()` (not part of TA module)

**Threading constraint:** `ib_insync` runs on a single-threaded asyncio event loop and is not thread-safe. All IB historical data fetches must happen on the main thread via `bulk_refresh()` BEFORE `parallel_fetch()` dispatches worker threads. Inside `parallel_fetch`, `get_snapshot()` only reads from DuckDB (thread-safe reads).

## IB Pacing

IB rate-limits historical data to ~60 requests per 10 minutes.

**`bulk_refresh()` strategy:**

- Queries all universe tickers for staleness in one DuckDB scan
- Batches stale tickers into groups of 55 (leaving headroom)
- Sleeps 10 minutes between batches
- Within each batch, sequential requests with 200ms spacing
- Cold-start backfill of ~500 tickers: ~90 minutes (one-time)
- Daily steady-state: only stale tickers need refresh. On a normal trading day after the first run, most are already current. Worst case (Monday after long weekend): all ~500 stale → same batching applies.

**Error handling:** On IB pacing error (error code 162), back off exponentially (10s, 20s, 40s, max 120s). After 5 consecutive failures for a single ticker, skip it and log a warning. After 3 consecutive batch-level failures, abort `bulk_refresh()` and proceed with whatever cache state exists (stale data is better than no scan).

## Testing

### Unit tests

- **`test_indicators.py`** — Feed known OHLC arrays into each TA-Lib wrapper, assert outputs match expected values. Freeze baselines from TA-Lib (not from old pandas code). Test edge cases: flat series (RSI → 50), all-up (RSI → 100), all-down (RSI → 0), short series (< warmup window → NaN).
- **`test_store.py`** — In-memory DuckDB (`:memory:`), test UPSERT idempotency, schema creation, staleness detection, read/write roundtrips, NOT NULL constraint enforcement, transaction atomicity (simulate crash between OHLC and indicator writes).

### Integration tests

- **`test_service.py`** — Mock `ib_client.get_historical_data()` with canned OHLC. Verify:
  - Full read-through flow: miss → fetch → compute → write → subsequent hit returns from DB without re-fetching IB
  - `get_snapshot()` returns dict with correct field names matching `trend_scan.py` expectations
  - Incremental update: append 1 new bar, verify indicators recomputed over full series
  - Stale detection: Monday pre-market correctly identifies Friday as last session
  - Holiday handling: no fetch triggered when cache matches last trading day
  - Stock split detection: 2:1 split triggers full re-fetch
  - Duplicate bar handling: re-fetching last bar UPSERTs cleanly
  - IB error handling: empty response, invalid contract, pacing error — all degrade gracefully

### E2E tests

- **`test_store_e2e.py`** — Real DuckDB file (temp path). Full lifecycle: create schema → write OHLC → write indicators → read back → append new bars → recompute indicators → verify history intact and queryable. Verify transaction atomicity: kill mid-write, verify DB is consistent on restart.
- **`test_trend_scan_regression.py`** — Run trend scan test suite after swap. Note: ADX/ATR values will differ (Wilder's smoothing vs rolling average). Tests should verify that scoring thresholds still produce reasonable pass/fail decisions, not exact float matches. Use `pytest.approx` with relaxed tolerance for ADX/ATR, strict for SMA/RSI/MACD.

## Dependencies

- `ta-lib` (Python wrapper) — requires TA-Lib C library installed (`brew install ta-lib` on macOS)
- No other new dependencies (DuckDB, pandas, ib_insync already in project)

## Out of Scope (v1)

- Multi-timeframe bars (schema supports it, not implemented)
- Charting API endpoint (future — will query `data/ta.duckdb` directly)
- Configurable indicator registry
- UW fallback for OHLC
- Concurrent writer support (v1 has single writer via `bulk_refresh`)
- Full indicator versioning (v1 uses `computed_at` timestamp only)
