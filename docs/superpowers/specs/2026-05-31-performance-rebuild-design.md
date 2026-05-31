# Performance Tab Rebuild — NAV-history backed, scope-aware, multi-broker

**Date:** 2026-05-31
**Status:** Draft v2 — tribunal-reviewed (Codex + Adversarial + Claude), pending implementation plan
**Author:** Brainstorm with chenxi

## Revision history

- **v1 (2026-05-31)** — Initial design. Issued for tribunal review.
- **v2 (2026-05-31)** — Tribunal review surfaced 22 issues. Critical fixes applied: real FUTU scope derivation, FUTU `account_env` mapping, NAV-vs-return semantics, EOD source labeling, type-level discriminated union, business-logic location, async engine, snapshot-date timezone, dual-curve PK protection.
- **v3 (2026-05-31)** — Second tribunal pass surfaced 11 patch-ups. Fixes: handler signature `request: Request`, lifespan re-warming of `app.state.futu_account`, FUTU connect-fallback `trd_env` read from matched row, atomic dual-curve protection via partial unique index, FUTU risk-metric masking until TWR ships, full union-aware updates to `performanceChart.ts` / `usePerformance.ts` / `PerformanceSummary` nullability, `schema.py` added to affected files, `load_benchmark_cached` signature takes pool, benchmark-unavailable warning string, `POST /performance/background` 202 semantics preserved, `_acc_id is None` guard inside `persist_futu_nav`.

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

| #   | Decision                          | Choice                                                                                                                                                                                                                                                                                                                                                                           | Reasoning                                                                                                                                                                                                                                                     |
| --- | --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Source of truth                   | `xenon.nav_history` (IB-recorded daily NAV)                                                                                                                                                                                                                                                                                                                                      | No reconstruction. No Flex Query dependency. No option-history-marked-to-zero failure mode.                                                                                                                                                                   |
| 2   | Compute location                  | Inline FastAPI service in `src/xenon/api/services/performance.py`, no subprocess                                                                                                                                                                                                                                                                                                 | `nav_history` is in Postgres. ~100 daily rows + ~100 SPY closes is a 100ms query, not a 180s subprocess job. Eliminates env-var inheritance. Matches `api/CLAUDE.md`.                                                                                         |
| 3   | Futu benchmark                    | SPY (same as IB)                                                                                                                                                                                                                                                                                                                                                                 | Operator chose SPY for tab-consistency. Beta/Alpha may read low if Futu holds non-US tickers; that is an explicit, documented choice.                                                                                                                         |
| 4   | Threshold ladder                  | `<5` sessions → "collecting history" empty state. `5 ≤ n < 30` → equity curve + hero only; risk metrics masked as "---". `n ≥ 30` → full panel.                                                                                                                                                                                                                                  | 5-day Sharpe is statistical noise (SE ≈ 7×). 30 sessions is the floor for any annualized risk metric. Both thresholds tunable via `XENON_PERF_MIN_DAYS_CURVE/METRICS`.                                                                                        |
| 5   | Period label                      | `YTD NAV Change` if inception ≤ Jan 2; `INCEPTION-TO-DATE NAV CHANGE` otherwise. "Inception" = earliest `nav_history.date` for the scope.                                                                                                                                                                                                                                        | Honest: this is NAV change, not TWR. Hero copy reflects that.                                                                                                                                                                                                 |
| 6   | Cache layer                       | Drop in-process cache in `/api/performance/route.ts`; thin scope-keyed memoize in the FastAPI service                                                                                                                                                                                                                                                                            | The subprocess dedup logic was a workaround for a 180s job. Inline service makes most of it unnecessary. Keep market-aware TTL (60s open / 30min closed).                                                                                                     |
| 7   | Deprecation                       | `xenon-portfolio-perf` deprecated; `xenon-perf-explainer` deprecated alongside (still reads the old JSON cache shape); old POST routes default `broker=IB` and proxy the new GET                                                                                                                                                                                                 | Single deprecation cohort, one release window.                                                                                                                                                                                                                |
| 8   | Metric semantics                  | v1 ships "NAV change" not TWR. IB scopes use `daily_pnl / prev_nav` (IB's `reqPnL().dailyPnL` excludes cash flows per IB API docs — TWS API `PnL` class). **FUTU scopes**: hero + equity curve only. Sharpe/Sortino/MaxDD/VaR/CVaR/Beta/Alpha/IR/Capture all masked as "---" with tooltip "FUTU TWR requires cash-flow tracking — follow-up." Same UX as the <30-session ladder. | IB's `dailyPnL` field is documented to exclude deposits. FUTU has no equivalent and a single deposit silently corrupts every annualized risk metric. Masking is honest; warning strings alone are not. Unmasked once cash-flow tracking ships in a follow-up. |
| 9   | FUTU `broker_account` source      | `app.state.futu_account` populated by: (a) FastAPI lifespan on boot, reading the most-recent FUTU `nav_history` row's `broker_account` so post-restart reads work, AND (b) `/futu/sync` handler refreshes it on every successful connect from `FutuClient._acc_id`. Pre-connect AND no prior nav row → `insufficient_history` with `reason="futu_not_synced"`                    | Lifespan warming closes the restart-regression hole. Singleton-refresh keeps it correct after manual reconnects.                                                                                                                                              |
| 10  | FUTU `account_env` mapping        | After connect, read `account_env` from the **matched account row** in the Futu account-list (`futu_client._matched_acc.trd_env`), NOT from the constructor argument. Map `REAL→"live"`, `SIMULATE→"sim"`. `resolve_from_env()` is updated to reject FUTU (it's IB-only) — FUTU scope can only be built via `persist_futu_nav` / `get_performance_scope`.                         | Connect-time fallback to first available account leaves `self.trd_env` mismatched with the actual account. Reading the matched row's env is the only correct source.                                                                                          |
| 11  | Snapshot date keying              | `current_session_date_et()` everywhere (IB + FUTU + benchmark). Defined in `src/xenon/utils/market_calendar.py` (or reused if it exists).                                                                                                                                                                                                                                        | IB sync already keys to ET. Mixing UTC and ET would cause same-day rows to land on different dates on non-ET hosts.                                                                                                                                           |
| 12  | Intraday vs close source labeling | New `nav_history.source` column: `'close'` or `'intraday'`. v1 writes only `'intraday'` (no EOD scheduler yet). v2 follow-up adds a 16:05 ET cron + flips closing rows.                                                                                                                                                                                                          | Lets us add EOD logic later without re-keying old rows. Service treats every row as authoritative for now; metric copy says "last observed".                                                                                                                  |
| 13  | Dual-curve protection             | **Atomic**: add `CREATE UNIQUE INDEX nav_history_one_env_per_day ON xenon.nav_history (broker, broker_account, date)` — excludes `account_env`. Plus app-level read-before-write guard for clean 409s. Defense-in-depth.                                                                                                                                                         | App-level guard alone is non-atomic — two concurrent writers can both see "no existing row" and insert different `account_env` rows. DB-level unique index makes the race impossible.                                                                         |
| 14  | Currency disclosure               | Panel hero appends `USD` next to the net-liq number. v1 does not separate FX P&L from instrument P&L.                                                                                                                                                                                                                                                                            | Futu's `net_liquidation` is queried with `currency=Currency.USD` hardcoded. The hero needs to disclose the unit so users aren't misled.                                                                                                                       |

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
    For v1 returns ALL rows regardless of source (v1 only writes 'intraday').
    v2 follow-up will add `prefer_source='close'` dedupe when EOD job ships.
  async def load_benchmark_cached(engine, ib_pool, symbol, period_start) -> tuple[pd.DataFrame, str | None]
    Returns (DataFrame, error_reason_or_None). Reads xenon.benchmark_closes;
    on any missing (symbol, date) row in the requested window, calls
    fetch_and_cache_benchmark with ib_pool's data role. Catches
    IBConnectionError, BadResponseError, etc.; on failure returns whatever
    rows ARE cached (possibly empty) plus a non-None error_reason so the
    service can surface "benchmark_unavailable: <reason>" in warnings.
  async def fetch_and_cache_benchmark(engine, ib_pool, symbol, missing_dates) -> None
    Uses ib_pool's "data" role via run_in_executor (ib_async is sync).
    UPSERTs into xenon.benchmark_closes. Failures bubble for caller to catch.

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
  summary: PerformanceSummary; // present only when status==="ok"; risk fields nullable (see below)
  series: PerformanceSeriesPoint[];
  warnings: string[]; // includes "benchmark_unavailable: <reason>" when benchmark fetch fails
  contracts_missing_history: string[];
}

// Risk metrics nullable to support: 5–30 day ladder (all brokers),
// FUTU at any tier (cash-flow contamination), benchmark-relative metrics
// when SPY cache is empty. Always-present fields: total_return, pnl,
// trading_days, ending_equity, starting_equity, max_drawdown,
// max_drawdown_duration_days, current_drawdown.
export interface PerformanceSummary {
  starting_equity: number;
  ending_equity: number;
  pnl: number;
  total_return: number;
  trading_days: number;
  max_drawdown: number;
  max_drawdown_duration_days: number;
  current_drawdown: number;
  // Annualized risk metrics — null when days < 30 OR broker === "FUTU"
  sharpe_ratio: number | null;
  sortino_ratio: number | null;
  calmar_ratio: number | null;
  annualized_return: number | null;
  annualized_volatility: number | null;
  downside_deviation: number | null;
  var_95: number | null;
  cvar_95: number | null;
  tail_ratio: number | null;
  ulcer_index: number | null;
  // Benchmark-relative — null when benchmark cache empty OR FUTU
  beta: number | null;
  alpha: number | null;
  correlation: number | null;
  r_squared: number | null;
  tracking_error: number | null;
  information_ratio: number | null;
  treynor_ratio: number | null;
  upside_capture: number | null;
  downside_capture: number | null;
  // Distribution — null for FUTU; populated for IB at ≥30 days
  hit_rate: number | null;
  positive_days: number | null;
  negative_days: number | null;
  flat_days: number | null;
  best_day: number | null;
  worst_day: number | null;
  average_up_day: number | null;
  average_down_day: number | null;
  win_loss_ratio: number | null;
  skew: number | null;
  kurtosis: number | null;
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
# src/xenon/api/server.py
# Step 1: change handler signature to accept request (currently `async def futu_sync():`)
@app.post("/futu/sync")
async def futu_sync(request: Request):           # <-- add request param
    ...
    # inside the existing `async with lock:` block, after _atomic_save:
    client = _futu_client
    if client is not None and client.is_connected() and client._acc_id is not None:
        # Read the *matched* account row's trd_env, not the constructor arg
        # (constructor arg may not match if connect() fell back to first account).
        matched_env = client._matched_acc.trd_env if client._matched_acc else client.trd_env
        # Cache scope identity on app.state for future GET /performance calls.
        request.app.state.futu_account = str(client._acc_id)
        request.app.state.futu_trd_env = matched_env
        await persist_futu_nav(
            engine=request.app.state.db_engine,
            futu_client=client,
            matched_trd_env=matched_env,
            payload=result,
        )
```

```python
# src/xenon/api/server.py lifespan — warm app.state.futu_account on boot
# so /performance?broker=FUTU works without a fresh /futu/sync after restart.
async with engine.begin() as conn:
    row = await conn.execute(
        sa.select(nav_history.c.broker_account, nav_history.c.account_env)
          .where(nav_history.c.broker == "FUTU")
          .order_by(nav_history.c.date.desc())
          .limit(1)
    )
    if r := row.first():
        app.state.futu_account = r.broker_account
        app.state.futu_trd_env = {"live": "REAL", "sim": "SIMULATE"}[r.account_env]
    else:
        app.state.futu_account = None
        app.state.futu_trd_env = None
```

```python
# src/xenon/api/services/futu_nav_persistence.py
async def persist_futu_nav(engine, futu_client, matched_trd_env, payload):
    # Hard guard against poison-row: _acc_id can flip to None mid-call after
    # a transient OpenD disconnect; str(None) would write the literal "None".
    if futu_client._acc_id is None:
        logger.warning("persist_futu_nav skipped: _acc_id is None")
        return
    net_liq = _safe_extract_net_liq(payload)  # returns None on missing/malformed
    if net_liq is None:
        logger.warning("persist_futu_nav skipped: payload missing net_liquidation")
        return

    scope = AccountScope(
        broker="FUTU",
        account_env=_env_from_trd_env(matched_trd_env),  # REAL→live, SIMULATE→sim
        broker_account=str(futu_client._acc_id),
    )
    today = current_session_date_et()

    async with engine.begin() as conn:
        # Decisions §13 — app-level guard for clean 409 (defense-in-depth;
        # DB-level unique index nav_history_one_env_per_day is the real fix).
        existing_env = await _account_env_for(conn, scope.broker, scope.broker_account, today)
        if existing_env is not None and existing_env != scope.account_env:
            raise NavAccountEnvConflict(scope, existing_env, today)  # → 409
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

-- Migration 3 — atomic dual-curve protection (Decisions §13)
CREATE UNIQUE INDEX nav_history_one_env_per_day
  ON xenon.nav_history (broker, broker_account, date);
-- Note: existing PK is (broker, account_env, broker_account, date). This
-- partial unique index EXCLUDES account_env, making it impossible for two
-- rows with different account_env values to coexist for the same
-- (broker, broker_account, date). The app-level read-before-write guard
-- stays as defense-in-depth and gives a clean 409 instead of an
-- IntegrityError.
```

The cross-`account_env` conflict is enforced both at the database (unique index — atomic) and at write time in `persist_futu_nav` and `ib_sync._append_nav_snapshot` (read-before-write — gives a clean 409).

`schema.py` is updated in this PR to add the `benchmark_closes` Table object and the new `source` Column on `nav_history` so query modules can import typed table/column references.

## Empty-state policy

| Condition                                | `status`               | `reason`          | Render                                                                                                                                                                                       |
| ---------------------------------------- | ---------------------- | ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| FUTU tab, singleton never synced         | `insufficient_history` | `futu_not_synced` | Header + hero (from cache if present) + primary CTA "Sync Futu to start collecting history"                                                                                                  |
| Scope is `legacy_unknown` (config drift) | `insufficient_history` | `scope_unset`     | Header + diagnostic copy + "Open settings"                                                                                                                                                   |
| 0 ≤ days_collected < 5                   | `insufficient_history` | `collecting`      | Header + hero (latest nav) + "COLLECTING HISTORY · {n} / 5 DAYS · curve unlocks at 5, metrics at 30"                                                                                         |
| 5 ≤ days_collected < 30                  | `ok`                   | —                 | Hero + equity curve. Sharpe/Sortino/Beta/Alpha/IR/Capture/VaR/CVaR cards render `---` with tooltip "minimum 30 sessions". MaxDD + drawdown chart render.                                     |
| days_collected ≥ 30, broker = IB         | `ok`                   | —                 | Full panel.                                                                                                                                                                                  |
| days_collected ≥ 30, broker = FUTU       | `ok`                   | —                 | Hero + equity curve + MaxDD + drawdown chart. Sharpe/Sortino/Beta/Alpha/IR/Capture/VaR/CVaR masked as `---` with tooltip "FUTU TWR requires cash-flow tracking — follow-up." (Decisions §8.) |

Currency unit displayed in the hero across all states.

For FUTU only, when `status="ok"` the warnings list includes:

> "FUTU NAV-change returns include external cash flows (deposits, withdrawals, dividends). True Time-Weighted Return requires cash-flow tracking — follow-up."

## Error handling

| Failure mode                                  | Response                                                                                                                                                                                                                                                                                                 |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Postgres unreachable                          | `502 {error: "performance database unavailable"}`. Panel renders `UNAVAILABLE`.                                                                                                                                                                                                                          |
| `broker` query param missing or invalid       | `400 {error: "broker must be IB or FUTU"}`. (Deprecated POST stubs default broker=IB before reaching this check.)                                                                                                                                                                                        |
| Scope resolves to `legacy_unknown`            | `200 status=insufficient_history reason=scope_unset`.                                                                                                                                                                                                                                                    |
| FUTU singleton never connected                | `200 status=insufficient_history reason=futu_not_synced` with optional hero from `data/futu_portfolio.json`.                                                                                                                                                                                             |
| Cross-`account_env` write collision           | Writer raises; FastAPI surfaces `409 {error: "nav account_env conflict for (broker, account)"}`. Logged for ops.                                                                                                                                                                                         |
| Benchmark cache hole for one mid-window day   | That day's `benchmark_close` and `benchmark_return` are null. Risk metrics over the window skip null rows (pandas `dropna` on the joined dataset).                                                                                                                                                       |
| Benchmark fetch fails entirely (IB pool down) | `load_benchmark_cached` returns `(cached_df_possibly_empty, error_reason)`. Service returns `status=ok` with benchmark fields null AND appends `"benchmark_unavailable: <reason>"` to `warnings`. Beta/Alpha/IR/Capture render `---`; panel surfaces the warning so the operator knows why.              |
| `POST /performance/background` 202 contract   | Preserved: handler returns 202 immediately with `{"status":"accepted"}`. The handler also kicks off a background task that warms the FastAPI service's scope-keyed memoize (defaults broker=IB). No-op semantically vs the GET — preserves the existing fire-and-forget contract that callers depend on. |
| Weekend / holiday gaps in `nav_history`       | Native — series is whatever dates exist. Daily-return math operates on actual consecutive rows.                                                                                                                                                                                                          |
| Single-day window                             | `insufficient_history` reason=`collecting`. Hero shows net_liq from the single row.                                                                                                                                                                                                                      |
| FUTU OpenD payload missing `net_liquidation`  | `persist_futu_nav` skips the write and logs a warning. No row added; performance continues to read the last good row.                                                                                                                                                                                    |

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
  - Cache miss with IB pool exception → no rows written; service returns (cached_df, error_reason).
  - Service includes `"benchmark_unavailable: <reason>"` in `warnings` when error_reason is non-null.
- `test_futu_persist_guard.py`:
  - `_acc_id = None` → persist_futu_nav returns early, logs warning, no row written.
  - payload missing `net_liquidation` → returns early, logs warning, no row written.
  - Cross-env conflict: two concurrent writers race → DB unique index raises IntegrityError → surfaced as 409 (DB-level enforcement test, separate from app-level guard test in `test_futu_nav_persistence.py`).
- `test_futu_account_warming.py`:
  - Lifespan boot with existing FUTU `nav_history` rows → `app.state.futu_account` and `app.state.futu_trd_env` populated.
  - Lifespan boot with NO FUTU rows → both set to None.
  - Post-restart `GET /performance?broker=FUTU` works against existing rows without requiring a fresh `/futu/sync`.

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
src/xenon/db/migrations/versions/<n>_add_benchmark_closes.py             NEW
src/xenon/db/migrations/versions/<n+1>_add_nav_history_source.py         NEW
src/xenon/db/migrations/versions/<n+2>_add_nav_history_unique_index.py   NEW (Decisions §13)
src/xenon/db/schema.py                         CHANGED (benchmark_closes Table, nav_history.source Column, unique index)
src/xenon/api/server.py                        CHANGED (POST /futu/sync gets `request: Request`, lifespan warms app.state.futu_account from latest FUTU nav_history row, deprecated POSTs default broker=IB, POST /performance/background keeps 202 by accepting+immediately returning before fanning out to GET handler)
src/xenon/api/guards.py                        CHANGED (new get_performance_scope dep)
src/xenon/execution/account_scope.py           CHANGED (resolve_from_env rejects FUTU per Decisions §10)
src/xenon/clients/futu_client.py               CHANGED (expose _matched_acc for trd_env lookup, OR add public trd_env_of_matched_account() helper)
src/xenon/execution/ib_sync.py                 CHANGED (account_env conflict guard added to _append_nav_snapshot)
src/xenon/reports/portfolio_performance.py     DEPRECATED (warning at import)
src/xenon/reports/performance_explainer_report.py DEPRECATED (warning at import)
web/lib/usePerformance.ts                      CHANGED (union-aware extractTimestamp: data.status === "ok" ? data.last_sync : null; broker in cache key)
web/lib/types.ts                               CHANGED (discriminated union; nullable risk-metric fields on PerformanceSummary)
web/lib/performanceChart.ts                    CHANGED (gate on data.status === "ok" before reading summary.starting_equity; skip null benchmark points in chart math)
web/components/PerformancePanel.tsx            CHANGED (status branch BEFORE destructure, currency in hero, fmtPct/fmtRatio handle null → "---", benchmark-unavailable warning rendered from data.warnings)
web/components/WorkspaceSections.tsx           CHANGED (forward activeAccount)
web/app/api/performance/route.ts               CHANGED (broker query param proxy)
scripts/tests/test_portfolio_performance.py    DELETED
scripts/tests/test_performance_lock.py         DELETED
```

### Formatter null-handling

The existing `fmtPct(value: number)` and `fmtRatio(value: number)` in `PerformancePanel.tsx` (and any extracted utils) MUST accept `number | null` and render `---` for null. Without this change, the 5–30 day ladder, FUTU panels, and benchmark-unavailable states would render `+0.00%` or `NaN%` instead of the intended `---`.

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
