# Performance Tab Rebuild — NAV-history backed, scope-aware, multi-broker

**Date:** 2026-05-31
**Status:** Draft v2 — tribunal-reviewed (Codex + Adversarial + Claude), pending implementation plan
**Author:** Brainstorm with chenxi

## Revision history

- **v1 (2026-05-31)** — Initial design. Issued for tribunal review.
- **v2 (2026-05-31)** — Tribunal review surfaced 22 issues. Critical fixes applied: real FUTU scope derivation, FUTU `account_env` mapping, NAV-vs-return semantics, EOD source labeling, type-level discriminated union, business-logic location, async engine, snapshot-date timezone, dual-curve PK protection.

## Problem

`/performance` is structurally broken in two independent ways.

1. **Half-baked equity reconstruction.** The current pipeline (`xenon-portfolio-perf`) tries to rebuild the YTD net-liquidation curve by replaying trade fills, marking each day with IB historical bars (stocks) and Unusual Whales option-contract history (options), then anchoring to today's net_liq. Live verification on 2026-05-31 produced a flat-line curve at $65,198.32 for every one of 102 trading days — `flat_days=100`, `positive_days=0`, `negative_days=1`, `skew=-10.05`, `kurtosis=101`. Degenerate metric values follow (Sharpe −1.58, Max DD −0.02%). Root causes visible in the script's own warnings: IB Flex Query token missing, Postgres-trades fallback path doesn't reconstruct marks correctly, and one contract (`STK:SPX`) has no daily history because SPX is an index but the script asks IB for a stock bar.
2. **Broker scope ignored.** `PerformancePanel` does not consume `activeAccount`. `/api/performance` sends no scope context. The FastAPI `POST /performance` route shells out to `xenon-portfolio-perf` as a subprocess that inherits whatever `XENON_TRADING_MODE` / `XENON_BROKER_ACCOUNT` were set when FastAPI booted. Switching tabs from IB to Futu visually moves the active-tab indicator but the performance numbers don't change — Futu's $224,683 net_liq tab still renders IB's $65,185 ending equity.

A third concern motivates this work: Futu has no historical performance path at all. The FastAPI `POST /futu/sync` handler (`server.py:2611`) fetches positions from Futu OpenD and writes `data/futu_portfolio.json`, but **does not** write to `nav_history` or `account_snapshots`. So there is no series to compute performance from even if the panel wiring were correct.

## Goals

- Replace the broken reconstruction with a NAV-history-backed source of truth.
- Make `/performance` scope-aware end-to-end so switching the IB/Futu tab actually re-renders the panel for that account.
- Persist FUTU NAV from the FastAPI `/futu/sync` hot path so the Futu tab has a curve to render (collected forward from day 0).
- Kill the 180-second subprocess for what is structurally a ~100ms DB query.
- Keep the existing `PerformancePanel` UI shell intact — the visual design works.
- Be honest about what the metric is. v1 ships **NAV change**, not Time-Weighted Return. The label and copy reflect that. TWR is a documented follow-up.

## Non-goals

- True Time-Weighted Return that subtracts external cash flows. Requires deposit/withdrawal tracking — IB Flex Query has it, but wiring is a separate spec.
- Per-trade attribution / Greeks decomposition.
- Backfilling historical IB `nav_history` for date ranges where rows are missing.
- Per-scope benchmark configuration. v1 uses SPY for both brokers (operator decision — see Decisions §3).
- Backfilling Futu history. v1 starts fresh from the day this lands; the panel shows "collecting history" until enough daily snapshots accumulate.
- Removing `xenon.reports.portfolio_performance`. The module and CLI stay registered with a deprecation warning; removal is a follow-up.
- Scheduled EOD snapshot job. Today's NAV row is whatever the last `/sync` produced (intraday or post-close); a real EOD scheduler is a follow-up. The `source` column on `nav_history` lets v2 add it without re-keying historical rows.

## Decisions locked in

| #   | Decision                          | Choice                                                                                                                                                                                       | Reasoning                                                                                                                                                              |
| --- | --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Source of truth                   | `xenon.nav_history` (IB-recorded daily NAV)                                                                                                                                                  | No reconstruction. No Flex Query dependency. No option-history-marked-to-zero failure mode.                                                                            |
| 2   | Compute location                  | Inline FastAPI service in `src/xenon/api/services/performance.py`, no subprocess                                                                                                             | `nav_history` is in Postgres. ~100 daily rows + ~100 SPY closes is a 100ms query, not a 180s subprocess job. Eliminates env-var inheritance. Matches `api/CLAUDE.md`.  |
| 3   | Futu benchmark                    | SPY (same as IB)                                                                                                                                                                             | Operator chose SPY for tab-consistency. Beta/Alpha may read low if Futu holds non-US tickers; that is an explicit, documented choice.                                  |
| 4   | Threshold ladder                  | `<5` sessions → "collecting history" empty state. `5 ≤ n < 30` → equity curve + hero only; risk metrics masked as "---". `n ≥ 30` → full panel.                                              | 5-day Sharpe is statistical noise (SE ≈ 7×). 30 sessions is the floor for any annualized risk metric. Both thresholds tunable via `XENON_PERF_MIN_DAYS_CURVE/METRICS`. |
| 5   | Period label                      | `YTD NAV Change` if inception ≤ Jan 2; `INCEPTION-TO-DATE NAV CHANGE` otherwise. "Inception" = earliest `nav_history.date` for the scope.                                                    | Honest: this is NAV change, not TWR. Hero copy reflects that.                                                                                                          |
| 6   | Cache layer                       | Drop in-process cache in `/api/performance/route.ts`; thin scope-keyed memoize in the FastAPI service                                                                                        | The subprocess dedup logic was a workaround for a 180s job. Inline service makes most of it unnecessary. Keep market-aware TTL (60s open / 30min closed).              |
| 7   | Deprecation                       | `xenon-portfolio-perf` deprecated; `xenon-perf-explainer` deprecated alongside (still reads the old JSON cache shape); old POST routes default `broker=IB` and proxy the new GET             | Single deprecation cohort, one release window.                                                                                                                         |
| 8   | Metric semantics                  | v1 ships "NAV change" not TWR. IB scopes use `daily_pnl / prev_nav` (IB's dailyPnL excludes cash flows). FUTU scopes use `(nav_today − nav_yesterday) / nav_yesterday` with a UI disclaimer. | IB's `reqPnL().dailyPnL` already excludes deposits. FUTU has no equivalent; honest disclosure beats wrong numbers.                                                     |
| 9   | FUTU `broker_account` source      | `app.state.futu_account` populated by the `/futu/sync` handler on first successful connect (from `FutuClient._acc_id`). Pre-connect → `insufficient_history` with `reason="futu_not_synced"` | The Futu singleton holds the account, but only after connect. Caching it on `app.state` keeps reads cheap and makes the lifecycle explicit.                            |
| 10  | FUTU `account_env` mapping        | Read `_futu_client.trd_env` and map `REAL→"live"`, `SIMULATE→"sim"`. Never inherit IB `app.state.trading_mode` for FUTU scopes.                                                              | Futu env and IB env are independent. The `AccountScope.account_env` literal already supports `"sim"` precisely for this case.                                          |
| 11  | Snapshot date keying              | `current_session_date_et()` everywhere (IB + FUTU + benchmark). Defined in `src/xenon/utils/market_calendar.py` (or reused if it exists).                                                    | IB sync already keys to ET. Mixing UTC and ET would cause same-day rows to land on different dates on non-ET hosts.                                                    |
| 12  | Intraday vs close source labeling | New `nav_history.source` column: `'close'` or `'intraday'`. v1 writes only `'intraday'` (no EOD scheduler yet). v2 follow-up adds a 16:05 ET cron + flips closing rows.                      | Lets us add EOD logic later without re-keying old rows. Service treats every row as authoritative for now; metric copy says "last observed".                           |
| 13  | Dual-curve protection             | `upsert_nav_history` raises if a row exists for `(broker, broker_account, date)` with a different `account_env`. Surfaced as 409.                                                            | The PK allows divergent `account_env` rows. A runtime check on write is simpler than a new unique constraint and gives a debuggable error.                             |
| 14  | Currency disclosure               | Panel hero appends `USD` next to the net-liq number. v1 does not separate FX P&L from instrument P&L.                                                                                        | Futu's `net_liquidation` is queried with `currency=Currency.USD` hardcoded. The hero needs to disclose the unit so users aren't misled.                                |

## Architecture

### Components

```
src/xenon/api/services/performance.py        (NEW, target ≤200 lines)
  async def compute(engine, scope, *, as_of=None) -> PerformanceData
  - Async; uses the FastAPI async engine via xenon.db.engine.get_engine().
  - Reads nav_history filtered by scope.
  - Reads SPY daily closes from xenon.benchmark_closes (cache).
  - Computes returns:
      IB: daily_pnl / prev_nav (excludes cash flows by IB's definition)
      FUTU: (nav_today − nav_yesterday) / nav_yesterday + UI disclaimer
  - Returns PerformanceData (discriminated union — see Type Contract).

src/xenon/api/services/futu_nav_persistence.py  (NEW shared helper, target ≤80 lines)
  async def persist_futu_nav(engine, futu_client, payload) -> None
  - Resolves scope: AccountScope("FUTU", env_from_trd_env(futu_client.trd_env), str(futu_client._acc_id))
  - Computes daily_pnl from prev-day row (never reads payload['daily_pnl'], which is lifetime).
  - Calls upsert_nav_history with source='intraday'.
  - Raises on cross-account_env collision (Decisions §13).
  - Called from BOTH FastAPI POST /futu/sync AND the xenon-futu-sync CLI via the same helper.

src/xenon/reports/performance_metrics.py     (NEW, target ≤150 lines)
  Pure functions (numpy/pandas; no I/O):
    sharpe(returns, rf, periods=252) -> float
    sortino(returns, rf, periods=252) -> float
    max_drawdown(equity) -> (depth, duration_days, trough_date)
    beta_alpha(returns, bench_returns) -> (beta, alpha)
    information_ratio(returns, bench_returns) -> (ir, tracking_error)
    upside_downside_capture(returns, bench_returns) -> (up, down)
    var_cvar(returns, percentile=0.05) -> (var, cvar)
    tail_ratio, ulcer, skew, kurtosis, hit_rate, ...
  Lifted from xenon.reports.portfolio_performance with no semantic change.

src/xenon/db/queries/nav_history.py          (NEW, target ≤80 lines)
  async def load_nav_curve(conn, scope, period_start) -> pd.DataFrame
    Columns: date, nav, daily_pnl, source. Sorted ascending. Scope-filtered.
  async def load_benchmark_cached(engine, symbol, period_start) -> pd.DataFrame
    Reads xenon.benchmark_closes. On any missing (symbol, date) row in the
    requested window, calls fetch_and_cache_benchmark (below). Catches
    IBConnectionError, BadResponseError, etc.; on failure returns whatever
    rows ARE cached (possibly empty) so the service can render the curve
    without benchmark.
  async def fetch_and_cache_benchmark(engine, symbol, missing_dates) -> None
    Uses the IB pool's "data" role via run_in_executor (ib_async is sync).
    UPSERTs into xenon.benchmark_closes. Failures swallowed and logged.

src/xenon/api/routes/performance.py          (NEW router, target ≤80 lines)
  GET /performance?broker=IB|FUTU
    Scope resolution via get_performance_scope dep:
      - broker missing/invalid → 400
      - broker=IB → AccountScope from app.state.{broker=IB, trading_mode, account}
      - broker=FUTU and app.state.futu_account is None → 200 with
        {status: "insufficient_history", reason: "futu_not_synced",
         hero_net_liq: <data/futu_portfolio.json's account_summary.net_liquidation or null>}
      - broker=FUTU and app.state.futu_account set → AccountScope(
          broker="FUTU",
          account_env=env_from_trd_env(app.state.futu_trd_env),
          broker_account=app.state.futu_account,
        )
    Body: PerformanceData (discriminated union).
    502: infra error.

  Deprecated:
    POST /performance         → defaults broker=IB, proxies the new GET
    POST /performance/background → same

web/lib/usePerformance.ts                    (CHANGED)
  Hook signature: usePerformance(active, activeAccount).
  Endpoint: `/api/performance?broker=${activeAccount.toUpperCase()}`.
  Cache key includes broker.

web/components/PerformancePanel.tsx          (CHANGED)
  Accepts activeAccount prop. Branches on data.status BEFORE destructuring
  summary. Renders three states: "ok" (full), "insufficient_history with
  reason='futu_not_synced'" (prompt to click sync), other insufficient_history
  (collecting). Hero shows currency unit.

web/components/WorkspaceSections.tsx         (CHANGED)
  Forwards activeAccount to PerformancePanel.

web/app/api/performance/route.ts             (CHANGED)
  Reads `broker` from URL search params; forwards to FastAPI GET.
  No in-process cache (FastAPI side caches if needed).
```

### Type contract (load-bearing — fixes the I-8/I-9 panel crash)

```typescript
// web/lib/types.ts
export type PerformanceData = PerformanceDataOk | PerformanceDataInsufficient;

export interface PerformanceDataOk {
  status: "ok";
  as_of: string;
  last_sync: string;
  period_start: string;
  period_end: string;
  period_label: string; // "YTD NAV Change" | "INCEPTION-TO-DATE NAV CHANGE"
  scope: { broker: "IB" | "FUTU"; account_env: string; broker_account: string };
  currency: "USD";
  benchmark: "SPY" | null; // null if cache empty
  benchmark_total_return: number | null;
  trades_source: "nav_history"; // legacy field kept for panel pill
  methodology: PerformanceMethodology;
  price_sources: PerformancePriceSources;
  summary: PerformanceSummary; // present only when status==="ok"
  series: PerformanceSeriesPoint[];
  warnings: string[];
  contracts_missing_history: string[];
}

export interface PerformanceDataInsufficient {
  status: "insufficient_history";
  reason: "scope_unset" | "futu_not_synced" | "collecting";
  days_collected: number;
  days_required_for_curve: number; // 5
  days_required_for_metrics: number; // 30
  inception_date: string | null;
  hero_net_liq: number | null;
  currency: "USD";
}

export interface PerformanceSeriesPoint {
  date: string;
  equity: number;
  daily_return: number | null;
  drawdown: number;
  benchmark_close: number | null; // null if cache hole
  benchmark_return: number | null; // null if cache hole
}
```

### Retired or downgraded

```
FastAPI POST /performance                     → deprecated stub (defaults broker=IB) for one release
FastAPI POST /performance/background          → deprecated stub for one release
xenon.reports.portfolio_performance.main()    → CLI prints deprecation warning
xenon.reports.performance_explainer_report:main → CLI deprecated alongside
scripts/tests/test_portfolio_performance.py   → deleted (tests retired impl)
scripts/tests/test_performance_lock.py        → deleted (tests POST-dedup behavior that no longer exists)
```

### Module size policy

If any new module exceeds its target line count during implementation, that is a signal to split before merging — typically by extracting another sub-module rather than continuing to grow the original.

## Data flow

**IB tab on `/performance`:**

1. `PerformancePanel` reads `activeAccount = "ib"`, calls `usePerformance(true, "ib")`.
2. Hook fetches `/api/performance?broker=IB`.
3. Next.js route proxies via `xenonFetch()` → FastAPI `GET /performance?broker=IB`.
4. `get_performance_scope` returns `AccountScope.from_app_state_ib(request.app.state)` (existing IB scope).
5. `performance.compute(engine, scope)`:
   - `load_nav_curve(conn, scope, period_start)` → DataFrame (date, nav, daily_pnl, source)
   - Decides threshold tier:
     - rows < 5: short-circuit → `status="insufficient_history", reason="collecting"`, hero from last row
     - 5 ≤ rows < 30: equity curve + drawdown only; risk metrics null
     - rows ≥ 30: full metric set
   - `load_benchmark_cached(engine, "SPY", period_start)` → DataFrame
   - Compute IB returns: `daily_pnl / prev_nav` (excludes cash flows)
   - Pure-math metrics from `performance_metrics`
   - Assemble PerformanceData
6. JSON returned, ~100ms.

**FUTU tab, futu singleton not connected yet (cold-start case):**

1–3. Same with `broker=FUTU`. 4. `get_performance_scope` checks `app.state.futu_account` → `None`. Returns 200 with `{status: "insufficient_history", reason: "futu_not_synced", hero_net_liq: <cached from data/futu_portfolio.json if any>, currency: "USD"}`. 5. Panel renders the "Click Sync to start collecting history" state with a primary CTA wired to `POST /futu/sync`.

**FUTU tab, post-sync:**

1–3. Same. 4. `get_performance_scope` builds `AccountScope("FUTU", env_from_trd_env(app.state.futu_trd_env), app.state.futu_account)`. 5. `performance.compute` runs. FUTU returns computed from nav delta with the deposit-flag warning in the `warnings` list.

## Persistence flow (FUTU NAV)

```python
# src/xenon/api/server.py, end of POST /futu/sync handler after successful fetch
from xenon.api.services.futu_nav_persistence import persist_futu_nav

# inside the existing `async with lock:` block, after _atomic_save:
client = _futu_client
if client is not None and client.is_connected() and client._acc_id is not None:
    # Cache scope identity on app.state for future GET /performance calls.
    request.app.state.futu_account = str(client._acc_id)
    request.app.state.futu_trd_env = client.trd_env
    await persist_futu_nav(
        engine=request.app.state.db_engine,
        futu_client=client,
        payload=result,
    )
```

```python
# src/xenon/api/services/futu_nav_persistence.py
async def persist_futu_nav(engine, futu_client, payload):
    scope = AccountScope(
        broker="FUTU",
        account_env=_env_from_trd_env(futu_client.trd_env),  # "REAL"→"live", "SIMULATE"→"sim"
        broker_account=str(futu_client._acc_id),
    )
    today = current_session_date_et()
    net_liq = float(payload["account_summary"]["net_liquidation"])

    async with engine.begin() as conn:
        # Decisions §13 — prevent dual-curve on the same (broker, broker_account, date).
        existing_env = await _account_env_for(conn, scope.broker, scope.broker_account, today)
        if existing_env is not None and existing_env != scope.account_env:
            raise ValueError(
                f"nav_history account_env conflict for {scope.broker}/{scope.broker_account} "
                f"on {today}: existing={existing_env!r}, attempting={scope.account_env!r}"
            )
        prev_nav = await _prev_nav(conn, scope, today)
        daily_pnl = (net_liq - prev_nav) if prev_nav is not None else None
        # NEVER read payload['daily_pnl'] — it's lifetime unrealized, not daily.
        await _upsert_nav_history(
            conn, scope=scope, date=today,
            nav=net_liq, daily_pnl=daily_pnl, source="intraday",
        )
```

## Schema changes

**Two Alembic migrations in this PR:**

```sql
-- Migration 1 — benchmark cache
CREATE TABLE xenon.benchmark_closes (
  symbol TEXT NOT NULL,
  date DATE NOT NULL,
  close NUMERIC(14, 4) NOT NULL,
  PRIMARY KEY (symbol, date)
);

-- Migration 2 — nav_history source labeling
ALTER TABLE xenon.nav_history
  ADD COLUMN source TEXT NOT NULL DEFAULT 'intraday'
  CHECK (source IN ('close', 'intraday'));
-- Existing rows backfilled to 'intraday' (honest about what they actually are).
```

No `nav_history` PK change. The cross-`account_env` conflict is enforced at write time in `persist_futu_nav` and `ib_sync._append_nav_snapshot` (which gets the same guard added).

## Empty-state policy

| Condition                                | `status`               | `reason`          | Render                                                                                                                                                   |
| ---------------------------------------- | ---------------------- | ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| FUTU tab, singleton never synced         | `insufficient_history` | `futu_not_synced` | Header + hero (from cache if present) + primary CTA "Sync Futu to start collecting history"                                                              |
| Scope is `legacy_unknown` (config drift) | `insufficient_history` | `scope_unset`     | Header + diagnostic copy + "Open settings"                                                                                                               |
| 0 ≤ days_collected < 5                   | `insufficient_history` | `collecting`      | Header + hero (latest nav) + "COLLECTING HISTORY · {n} / 5 DAYS · curve unlocks at 5, metrics at 30"                                                     |
| 5 ≤ days_collected < 30                  | `ok`                   | —                 | Hero + equity curve. Sharpe/Sortino/Beta/Alpha/IR/Capture/VaR/CVaR cards render `---` with tooltip "minimum 30 sessions". MaxDD + drawdown chart render. |
| days_collected ≥ 30                      | `ok`                   | —                 | Full panel.                                                                                                                                              |

Currency unit displayed in the hero across all states.

For FUTU only, when `status="ok"` the warnings list includes:

> "FUTU NAV-change returns include external cash flows (deposits, withdrawals, dividends). True Time-Weighted Return requires cash-flow tracking — follow-up."

## Error handling

| Failure mode                                  | Response                                                                                                                                                               |
| --------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Postgres unreachable                          | `502 {error: "performance database unavailable"}`. Panel renders `UNAVAILABLE`.                                                                                        |
| `broker` query param missing or invalid       | `400 {error: "broker must be IB or FUTU"}`. (Deprecated POST stubs default broker=IB before reaching this check.)                                                      |
| Scope resolves to `legacy_unknown`            | `200 status=insufficient_history reason=scope_unset`.                                                                                                                  |
| FUTU singleton never connected                | `200 status=insufficient_history reason=futu_not_synced` with optional hero from `data/futu_portfolio.json`.                                                           |
| Cross-`account_env` write collision           | Writer raises; FastAPI surfaces `409 {error: "nav account_env conflict for (broker, account)"}`. Logged for ops.                                                       |
| Benchmark cache hole for one mid-window day   | That day's `benchmark_close` and `benchmark_return` are null. Risk metrics over the window skip null rows (pandas `dropna` on the joined dataset).                     |
| Benchmark fetch fails entirely (IB pool down) | `load_benchmark_cached` returns whatever rows ARE cached (possibly empty). Service returns `status=ok` with benchmark fields null. Beta/Alpha/IR/Capture render `---`. |
| Weekend / holiday gaps in `nav_history`       | Native — series is whatever dates exist. Daily-return math operates on actual consecutive rows.                                                                        |
| Single-day window                             | `insufficient_history` reason=`collecting`. Hero shows net_liq from the single row.                                                                                    |
| FUTU OpenD payload missing `net_liquidation`  | `persist_futu_nav` skips the write and logs a warning. No row added; performance continues to read the last good row.                                                  |

## Testing

Per CLAUDE.md the project targets 95% coverage.

**Python (pytest):**

- `test_performance_metrics.py` — pure-math fixtures (fixed NumPy arrays → known Sharpe/Sortino/DD values). Stable golden values.
- `test_performance_service.py`:
  - Happy path IB: 102 nav_history rows + 102 SPY closes → metric values match a known good output.
  - Threshold ladder: 4 rows → status=insufficient_history reason=collecting. 10 rows → status=ok, summary.sharpe is null. 35 rows → status=ok, summary.sharpe is non-null.
  - Scope isolation: IB rows + FUTU rows for same dates; IB call returns IB-only, FUTU call returns FUTU-only, and the two equity series differ.
  - FUTU cold-start: app.state.futu_account is None → status=insufficient_history reason=futu_not_synced.
  - FUTU post-sync: app.state.futu_account set, nav rows exist → status=ok.
  - Benchmark missing entirely: SPY rows absent → status=ok, benchmark fields null.
  - Benchmark partial: SPY missing one mid-window day → that day's bench_return is null; Sharpe still computed.
  - Returns formula: IB uses daily_pnl/prev_nav; FUTU uses (nav_t − nav_t-1)/nav_t-1.
- `test_futu_nav_persistence.py`:
  - First call inserts (broker=FUTU, account_env=live for REAL trd_env).
  - Same-day second call updates last-write-wins.
  - Next-day call inserts new row, daily_pnl = today_nav − yesterday_nav.
  - Cross-env conflict: existing row has account_env="live"; new write with "sim" → raises.
  - payload['daily_pnl'] (lifetime unrealized) is IGNORED — assert the computed daily_pnl differs from payload['daily_pnl'].
- `test_performance_route.py`:
  - `GET /performance?broker=IB` → 200, scope resolved from app.state.
  - `GET /performance?broker=FUTU` cold-start → 200, status=insufficient_history reason=futu_not_synced.
  - `GET /performance?broker=FUTU` post-sync → 200 status=ok.
  - `GET /performance` (no broker) → 400.
  - `GET /performance?broker=GARBAGE` → 400.
  - `POST /performance` (deprecated stub) → 200 with IB payload (defaults broker=IB).
- `test_benchmark_cache.py`:
  - Cache miss → IB pool fetch → rows upserted.
  - Cache miss with IB pool exception → no rows written, no exception propagated.

**Web (Vitest):**

- `usePerformance.test.ts` — switching `activeAccount` re-fetches with the new broker query param; cache keys are distinct per broker.
- `PerformancePanel.test.tsx` — renders correctly for each branch: status=ok, status=insufficient_history reason=collecting, reason=futu_not_synced, reason=scope_unset. Currency disclosed in hero.

**E2E (Playwright / chrome-cdp):**

- `performance-broker-switch.spec.ts` — load `/performance`, observe IB number; click Futu tab; assert the hero number changes (or the empty state appears) AND that the two values are NOT equal.
- `performance-futu-cold-start.spec.ts` — fresh boot (no /futu/sync), Futu tab → "Sync Futu to start collecting history" CTA visible. Click → /futu/sync runs → curve unlocks once enough days are collected (uses fixtures for the day-N case).

## Affected files

```
src/xenon/api/routes/performance.py            NEW
src/xenon/api/services/performance.py          NEW
src/xenon/api/services/futu_nav_persistence.py NEW
src/xenon/db/queries/nav_history.py            NEW
src/xenon/reports/performance_metrics.py       NEW (extracted)
src/xenon/db/migrations/versions/<n>_add_benchmark_closes.py        NEW
src/xenon/db/migrations/versions/<n+1>_add_nav_history_source.py    NEW
src/xenon/api/server.py                        CHANGED (POST /futu/sync, deprecated POSTs, app.state.futu_*)
src/xenon/api/guards.py                        CHANGED (new get_performance_scope dep, or new module)
src/xenon/execution/ib_sync.py                 CHANGED (account_env conflict guard added to _append_nav_snapshot)
src/xenon/reports/portfolio_performance.py     DEPRECATED (warning at import)
src/xenon/reports/performance_explainer_report.py DEPRECATED (warning at import)
web/lib/usePerformance.ts                      CHANGED
web/lib/types.ts                               CHANGED (discriminated union)
web/components/PerformancePanel.tsx            CHANGED (status branch before destructure, currency in hero)
web/components/WorkspaceSections.tsx           CHANGED (forward activeAccount)
web/app/api/performance/route.ts               CHANGED (broker query param proxy)
scripts/tests/test_portfolio_performance.py    DELETED
scripts/tests/test_performance_lock.py         DELETED
```

## Out of scope (follow-ups)

- True Time-Weighted Return with cash-flow tracking (deposits/withdrawals/dividends columns on `nav_history`, sourced from IB Flex Query and Futu cashflow API).
- Scheduled EOD snapshot job — a 16:05 ET cron that writes `nav_history` rows with `source='close'` and flips the day's prior `'intraday'` row to `'close'` if no separate close arrives.
- Per-trade attribution overlay using the retired trade-replay logic.
- Backfilling historical `nav_history` rows for IB scopes where rows are missing.
- Per-scope benchmark configuration.
- Backfilling Futu history from any external source.
- Removing the deprecated `xenon-portfolio-perf` and `xenon-perf-explainer` CLIs and the no-op POST routes.

## Order-path guards

This change does not touch the order path. No updates required to `scripts/checks/order_path_caller_allowlist.py` or `scripts/checks/no_json_fallback_on_order_path.py`. Performance is a read-only surface.

## Release / rollout

- Single PR. Two Alembic migrations land together.
- Deprecated POST routes proxy the new GET; existing callers that don't pass `broker` are coerced to `broker=IB` so they keep working.
- New Futu `nav_history` rows accrete naturally as `/futu/sync` runs. Existing IB `nav_history` rows reused as-is. Per Decisions §12 they're back-labeled `source='intraday'` (honest about what they actually are).
- Visual regression — verify in browser per `web/CLAUDE.md` E2E rule. Both broker tabs. Cold-start (no Futu sync) and post-sync.
- Rollback — revert the PR. The two migrations are additive (new table, new nullable-with-default column) so a forward-only deploy is safe; rollback can leave the schema in place or drop both in a follow-up.
