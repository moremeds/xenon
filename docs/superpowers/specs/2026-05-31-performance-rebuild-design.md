# Performance Tab Rebuild — NAV-history backed, scope-aware, multi-broker

**Date:** 2026-05-31
**Status:** Draft — pending implementation plan
**Author:** Brainstorm with chenxi

## Problem

`/performance` is structurally broken in two independent ways.

1. **Half-baked equity reconstruction.** The current pipeline (`xenon-portfolio-perf`) tries to rebuild the YTD net-liquidation curve by replaying trade fills, marking each day with IB historical bars (stocks) and Unusual Whales option-contract history (options), then anchoring to today's net_liq. Live verification on 2026-05-31 produced a flat-line curve at $65,198.32 for every one of 102 trading days — `flat_days=100`, `positive_days=0`, `negative_days=1`, `skew=-10.05`, `kurtosis=101`. Degenerate metric values follow (Sharpe −1.58, Max DD −0.02%). Root causes visible in the script's own warnings: IB Flex Query token missing, Postgres-trades fallback path doesn't reconstruct marks correctly, and one contract (`STK:SPX`) has no daily history because SPX is an index but the script asks IB for a stock bar.
2. **Broker scope ignored.** `PerformancePanel` does not consume `activeAccount`. `/api/performance` sends no scope context. The FastAPI `POST /performance` route shells out to `xenon-portfolio-perf` as a subprocess that inherits whatever `XENON_TRADING_MODE` / `XENON_BROKER_ACCOUNT` were set when FastAPI booted. Switching tabs in the UI from IB to Futu visually moves the active-tab indicator but the performance numbers don't change — Futu's $224,683 net_liq tab still renders IB's $65,185 ending equity.

A third concern motivates this work: Futu has no historical performance path at all. `futu_sync` reads positions from Futu OpenD, prints `net_liq=$X` to the log, and exits. Nothing is persisted to `nav_history` or `account_snapshots` for the FUTU scope, so there is no series to compute performance from even if we wired the panel correctly.

## Goals

- Replace the broken reconstruction with a NAV-history-backed source of truth (`xenon.nav_history`, already written daily by `ib_sync`).
- Make `/performance` scope-aware end-to-end so switching the IB/Futu tab actually re-renders the panel for that account.
- Persist Futu NAV from `futu_sync` so the Futu tab has a curve to render (collected forward from day 0).
- Kill the 180-second subprocess for what is structurally a ~100ms DB query.
- Keep the existing `PerformancePanel` UI shell intact — the visual design works.

## Non-goals

- Per-trade attribution / Greeks decomposition. The current `xenon-portfolio-perf` script's trade-replay logic could feed an attribution overlay later; out of scope here.
- Backfilling historical IB `nav_history` for date ranges where rows are missing. If gaps exist, a one-shot script that walks `account_snapshots.payload` is a follow-up.
- Per-scope benchmark configuration. v1 uses SPY for both brokers (operator decision — see Decisions §3).
- Backfilling Futu history. v1 starts fresh from the day this lands; the panel shows a "collecting history" empty state until five daily snapshots are recorded.
- Removing `xenon.reports.portfolio_performance`. The module and CLI stay registered with a deprecation warning; removal is a follow-up after the new path is verified in prod.

## Decisions locked in

| #   | Decision                 | Choice                                                                                                              | Reasoning                                                                                                                                                                |
| --- | ------------------------ | ------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1   | Source of truth          | `xenon.nav_history` (IB-recorded daily NAV)                                                                         | Matches IB statements byte-for-byte. No reconstruction. No Flex Query dependency. No option-history-marked-to-zero failure mode.                                         |
| 2   | Compute location         | Inline FastAPI service, no subprocess                                                                               | nav_history is in Postgres. ~100 daily rows + ~100 SPY closes is a 100ms query, not a 180s subprocess job. Eliminates env-var inheritance — the source of the scope bug. |
| 3   | Futu benchmark           | SPY (same as IB)                                                                                                    | Operator chose SPY for tab-consistency. Beta/Alpha may read low if Futu holds non-US tickers; that is an explicit, documented choice, not a bug.                         |
| 4   | Futu history bootstrap   | Start fresh; "collecting history" empty state until ≥5 daily snapshots                                              | Honest about the limitation. No fake data. Threshold tunable via `XENON_PERF_MIN_DAYS`.                                                                                  |
| 5   | Period label             | `YTD` if inception ≤ Jan 2; `INCEPTION TO DATE` otherwise. "Inception" = earliest `nav_history.date` for the scope. | Futu and any post-Jan-2 IB scope get the inception label. Keeps the hero copy accurate.                                                                                  |
| 6   | Cache layer              | Drop in-process cache in `/api/performance/route.ts`; thin scope-keyed memoize FastAPI-side                         | The subprocess dedup logic was a workaround for a 180s job. Inline service makes most of it unnecessary. Keep market-aware TTL (60s open / 30min closed).                |
| 7   | Existing CLI entry point | `xenon-portfolio-perf` deprecated, not removed                                                                      | One-release deprecation window. CLI prints a warning; old POST routes stay as no-op stubs that proxy the new GET payload.                                                |

## Architecture

### Components

```
src/xenon/reports/performance_service.py     (NEW, target ≤200 lines)
  compute(scope: AccountScope, *, as_of: date | None = None) -> PerformanceData
  - Reads xenon.nav_history filtered by scope.
  - Reads SPY daily closes via the benchmark loader.
  - Returns PerformanceData. status ∈ {"ok", "insufficient_history"}.
  - Pure orchestration; no metric math inline.

src/xenon/reports/performance_metrics.py     (NEW, target ≤150 lines)
  Extracted pure functions:
    sharpe(returns, rf, periods=252) -> float
    sortino(returns, rf, periods=252) -> float
    max_drawdown(equity) -> (depth, duration_days, trough_date)
    beta_alpha(returns, bench_returns) -> (beta, alpha)
    information_ratio(returns, bench_returns) -> (ir, tracking_error)
    upside_downside_capture(returns, bench_returns) -> (up, down)
    var_cvar(returns, percentile=0.05) -> (var, cvar)
    tail_ratio, ulcer, skew, kurtosis, hit_rate, ...
  Lifted from xenon.reports.portfolio_performance with no semantic change.
  All take numpy arrays; no I/O.

src/xenon/db/queries/nav_history.py          (NEW, target ≤80 lines)
  load_nav_curve(conn, scope, period_start) -> pd.DataFrame
    Columns: date, nav, daily_pnl. Sorted ascending. Scope-filtered.
  load_benchmark(conn, symbol, period_start) -> pd.DataFrame
    Columns: date, close. Reads from a new `xenon.benchmark_closes` cache
    table (broker-agnostic; keyed by `(symbol, date)`). Cache is populated
    on first miss by fetching daily bars via the IB pool's data role
    (clientId 0-9, already established) — same pattern xenon-portfolio-perf
    used, but cached in PG instead of fetched per-request.

src/xenon/api/routes/performance.py          (NEW router, target ≤80 lines)
  GET /performance?broker=IB|FUTU
    Scope resolution:
      - broker=IB: AccountScope from app.state (existing IB boot scope)
      - broker=FUTU: AccountScope(FUTU, app.state.trading_mode, futu_account_singleton())
      - missing/invalid broker: 400
    200 → PerformanceData with status="ok"
    200 → PerformanceData with status="insufficient_history" (+ days_collected,
          days_required, inception_date, hero_net_liq)
    502 → {error: "..."} for unrecoverable infrastructure failures

  New dependency `get_performance_scope(broker: str, request: Request) -> AccountScope`
  lives in this router file (or in xenon.api.guards if reused later). It
  composes the existing app.state-driven scope with the request-specified broker.

web/lib/usePerformance.ts                    (CHANGED)
  Hook signature: usePerformance(active: boolean, activeAccount: "ib"|"futu").
  Endpoint becomes `/api/performance?broker={IB|FUTU}`. Cache key includes broker.

web/components/PerformancePanel.tsx          (CHANGED)
  Accepts activeAccount prop. Passes through to usePerformance.
  New branch for status="insufficient_history".

web/app/api/performance/route.ts             (CHANGED)
  Becomes a thin proxy. Reads `broker` query param, forwards to FastAPI.
  No in-process cache (FastAPI side caches if needed).
```

### Retired or downgraded

```
FastAPI POST /performance                     → kept as deprecated stub for one release
FastAPI POST /performance/background          → kept as deprecated stub for one release
xenon.reports.portfolio_performance.main()    → CLI prints deprecation warning; no behavior change
```

### Module size policy

If any new module exceeds its target line count during implementation, that is a signal to split before merging — typically by extracting another sub-module under `src/xenon/reports/perf/` rather than continuing to grow the original.

## Data flow

A user on `/performance` with the Futu tab active triggers:

1. `PerformancePanel` reads `activeAccount = "futu"` from `useActiveAccount()` and calls `usePerformance("futu")`.
2. `usePerformance` requests `/api/performance?broker=FUTU`.
3. `web/app/api/performance/route.ts` proxies via `xenonFetch()` to FastAPI `GET /performance?broker=FUTU`. The `broker` query param flows through.
4. FastAPI's new `get_performance_scope` dependency composes `app.state.trading_mode` (the boot-time mode) with `broker=FUTU` and the Futu account known to the `futu_sync` singleton → `AccountScope(broker="FUTU", account_env="live", broker_account="28175...3263")`.
5. `performance_service.compute(scope)`:
   - `load_nav_curve(conn, scope, period_start=jan_2_or_inception)` → DataFrame
   - If rows < 5: short-circuit, return `status="insufficient_history"` with `hero_net_liq` from the latest row (or from `account_snapshots.payload.net_liquidation` if no nav row exists yet).
   - Otherwise: `load_benchmark(conn, "SPY", period_start)` → DataFrame
   - Pure-math metrics from `performance_metrics`
   - Assemble PerformanceData (same JSON shape the panel already consumes)
6. JSON returned. ~100ms target.

## Futu NAV persistence

`src/xenon/execution/futu_sync.py` writes one `nav_history` row per call:

```python
upsert_nav_history(
    conn,
    broker="FUTU",
    account_env=scope.account_env,
    broker_account=scope.broker_account,
    date=date.today(),               # UTC date — same convention as IB
    nav=acct["net_liquidation"],
    daily_pnl=nav - prev_day_nav if prev_day_nav is not None else None,
    # total/cash/stock_value/options_value left NULL — IB-only breakdown
)
```

The upsert key `(broker, account_env, broker_account, date)` matches the existing PK. Last-write-wins behavior within a single trading day is acceptable — the last observed net_liq is the close-of-day value for performance purposes.

No `nav_history` schema migration is required — `ck_nav_broker IN ('IB', 'FUTU')` and nullable breakdown columns already exist (migrations `27a1d085c2cd_add_broker_account_scope_columns.py` and `2026_05_03_extend_nav_history_breakdown.py`).

One new table is added in this PR via Alembic:

```sql
CREATE TABLE xenon.benchmark_closes (
  symbol TEXT NOT NULL,
  date DATE NOT NULL,
  close NUMERIC(14, 4) NOT NULL,
  PRIMARY KEY (symbol, date)
);
```

Populated lazily by the benchmark loader on first miss for each `(symbol, date)`. Broker-agnostic. Eliminates the per-request IB historical-bar fetch the current script does on every page load.

## Empty-state policy

When `days_collected < XENON_PERF_MIN_DAYS` (default 5), the panel renders a single section with:

- Period label: `INCEPTION TO DATE`
- Progress pill: `COLLECTING HISTORY · {days_collected} / {days_required} DAYS`
- Hero number: latest `nav_history.nav` (or current `account_snapshots.payload.net_liquidation` if no nav row exists yet — day-0 case)
- Inception date
- Single line of copy: "Metrics will compute once {days_required} daily snapshots are recorded."

No chart, no metric cards, no warnings list. The visual shell stays consistent (same `section` wrapping) so the page doesn't look broken.

## Error handling

| Failure mode                                     | Response                                                                                                                                          |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| Postgres unreachable                             | `502 {error: "performance database unavailable"}`. Panel renders existing `UNAVAILABLE` empty state.                                              |
| Scope resolves to `legacy_unknown`               | `200 {status: "insufficient_history", days_collected: 0, reason: "scope_unset"}`. Panel prompts the user to set active account.                   |
| `nav_history` has rows but benchmark fetch fails | `200 {status: "ok", ...}` with benchmark fields null. Panel renders `---` for Beta/Alpha/IR/Capture/Correlation. Equity curve alone still useful. |
| Weekend / holiday gaps in `nav_history`          | Native — series is whatever calendar dates exist. No synthetic gap-fills. Daily-return math operates on actual consecutive rows.                  |
| Single-day window                                | Returns `insufficient_history`. Day-0 hero shows net_liq from the single row.                                                                     |

## Testing

Per CLAUDE.md the project targets 95% coverage. Required tests:

**Python (pytest):**

- `test_performance_metrics.py` — pure-math fixtures (fixed NumPy arrays → known Sharpe/Sortino/DD values). Lifted from any existing tests against `portfolio_performance.py` where they exist.
- `test_performance_service.py`:
  - Scope filtering: rows for IB + rows for FUTU on same dates; IB scope returns IB-only.
  - Insufficient history: scope with 3 rows → `status="insufficient_history"`.
  - Benchmark missing: SPY loader raises → service returns `status="ok"` with benchmark fields null.
  - Happy path: 102 rows → metric values match expected output.
- `test_futu_sync_nav_persistence.py`:
  - First sync of the day inserts a row.
  - Second sync same day updates (last-write-wins) — verify row count stays at 1.
  - Second day inserts a new row, `daily_pnl = nav_today - nav_yesterday`.
- `test_performance_route.py`:
  - Scope dependency resolves correctly from headers.
  - 200 with correct data shape.
  - 502 on simulated DB error.

**Web (Vitest):**

- `usePerformance.test.ts` — switching `activeAccount` re-fetches with new headers; cache keys are distinct per account.
- `PerformancePanel.test.tsx` — renders empty state when API returns `status="insufficient_history"`; renders full panel when `status="ok"`.

**E2E (Playwright / chrome-cdp):**

- `performance-broker-switch.spec.ts` — load `/performance`, observe IB number; click Futu tab; assert the hero number changes (or the collecting-history state appears). Reuses the chrome-cdp pattern documented in `web/CLAUDE.md`.

## Out of scope (follow-ups)

- Per-trade attribution overlay using the retired trade-replay logic.
- Backfilling historical `nav_history` rows for IB scopes where rows are missing (one-shot script over `account_snapshots`).
- Per-scope benchmark configuration (Decisions §3 — SPY-everywhere for v1).
- Backfilling Futu history from any external source.
- Removing the deprecated `xenon-portfolio-perf` CLI and the no-op POST routes.

## Order-path guards

This change does not touch the order path. No updates required to `scripts/checks/order_path_caller_allowlist.py` or `scripts/checks/no_json_fallback_on_order_path.py`. Performance is a read-only surface.

## Release / rollout

- Single PR. No dual-write window; the new GET route is independent of the old POST route, and the old POST routes proxy the new GET payload for one release.
- One Alembic migration adds `xenon.benchmark_closes`. No data migration for existing rows. New Futu `nav_history` rows accrete naturally as `futu_sync` runs. Existing IB `nav_history` rows are reused as-is.
- Visual regression — verify in browser per `web/CLAUDE.md` E2E rule. Both broker tabs.
- Rollback — revert the PR. The old POST routes / subprocess path remain runnable until the follow-up cleanup PR.
