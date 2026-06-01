# Performance Tab Rebuild — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken `xenon-portfolio-perf` subprocess pipeline with an inline FastAPI service that reads `xenon.nav_history` directly, persist FUTU NAV from the FastAPI `/futu/sync` hot path, and make `/performance` scope-aware so the IB/Futu tab switch actually re-renders the panel for that account. Per `docs/superpowers/specs/2026-05-31-performance-rebuild-design.md` (v3.1).

**Architecture:** Phase 0 is a verification gate that confirms IB `dailyPnL` cash-flow semantics empirically — every later phase that computes IB risk metrics is gated behind it. Phase 1 lands three additive Alembic migrations (`benchmark_closes` table, `nav_history.source` column, `nav_history_one_env_per_day` partial unique index) plus schema.py reflections. Phase 2 fixes a pre-existing `FutuClient` silent-env-lie bug, adds the `persist_futu_nav` shared helper, and adds a cross-env conflict guard to IB `nav_history` writes. Phase 3 extracts pure-math `performance_metrics`, builds the `nav_history.py` query module, and ships the new `performance.py` service with Phase-0 env branching + low-confidence math. Phase 4 wires the new GET route, the lifespan warming of `app.state.futu_account`, and the FUTU sync hot-path. Phase 5 updates the web contract end-to-end (types, hook, chart math, panel branching, route proxy). Phase 6 retires deprecated CLIs and old tests. Phase 7 verifies the full flow in a real browser per `web/CLAUDE.md`.

**Tech Stack:** Python 3.13 (via `uv`), FastAPI, SQLAlchemy + Alembic, Postgres 15, Next.js 15, React 19, Vitest, Playwright (or chrome-cdp).

**Spec reference:** Each task cites `docs/superpowers/specs/2026-05-31-performance-rebuild-design.md` ("the spec" below). Read the spec section before implementing if anything is unclear — the plan is a sequence of bite-sized actions; the spec has the full design rationale.

---

## ⚠️ PRE-EXECUTION CORRECTIONS (read first — applied 2026-06-01)

After writing the plan, three independent reviews (self, Codex CLI, Claude integration audit, adversarial agent) surfaced corrections that the executor MUST apply _in place of_ the original task text where they conflict. The body of the plan below has NOT been rewritten — these corrections are the authoritative deltas.

### Critical — will cause runtime/test failure if executed verbatim

| #   | Site                                                                          | Defect                                                                                                                                                                                                                                                                                                                                                                                                     | Correction                                                                                                                                                                                                                                                                                                                                                   |
| --- | ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1   | All tasks that import `current_session_date_et`                               | The function does NOT exist in `src/xenon/utils/market_calendar.py`. Verified via grep — only `load_holidays`, `is_market_open`, `get_last_n_trading_days`, `_is_trading_day` are defined.                                                                                                                                                                                                                 | Add this function as Phase 1 prerequisite (new Task 1.0). Body: `def current_session_date_et() -> date: import pytz; return datetime.now(pytz.timezone("America/New_York")).date()`. Test: assert returns same day as IB Gateway's session-date.                                                                                                             |
| 2   | Task 2.4 `_append_nav_snapshot` patch                                         | Actual signature is `_append_nav_snapshot(net_liq: float, daily_pnl=None) -> None` — takes no `engine`/`scope`/`date_` args. Function derives scope from `_scope_from_env()` and date from `datetime.now(et).date()` internally.                                                                                                                                                                           | Patch INSIDE the function body. Inject the cross-env guard after the existing `today=...` and `broker, account_env, broker_account = _scope_from_env()` lines, before the upsert. Use the locally-resolved scope vars, not function params.                                                                                                                  |
| 3   | Task 1.1 schema.py reflection                                                 | The actual `src/xenon/db/schema.py` uses imported `Table`, `Column`, `Text`, `Numeric`, `Date` and the metadata is named `xenon_metadata` — NOT `sa.Table`/`sa.Column`/`metadata`.                                                                                                                                                                                                                         | Change `benchmark_closes = sa.Table("benchmark_closes", metadata, sa.Column(...))` to `benchmark_closes = Table("benchmark_closes", xenon_metadata, Column("symbol", Text, primary_key=True), Column("date", Date, primary_key=True), Column("close", Numeric(14,4), nullable=False), schema="xenon")`. Verify the existing imports at the top of schema.py. |
| 4   | Task 4.1 `get_performance_scope`                                              | `request.app.state.broker_account` does NOT exist. `server.py` exposes `app.state.account` and `app.state.trading_mode`.                                                                                                                                                                                                                                                                                   | Replace `broker_account=request.app.state.broker_account` with `broker_account=request.app.state.account`. (Or use `xenon.execution.account_scope.resolve_from_app_state(request.app.state)` if that helper exists — verify.)                                                                                                                                |
| 5   | Task 4.2 lifespan warming snippet                                             | The example uses an undefined local `engine` variable. Actual server lifespan stores the engine on `app.state.db_engine` via `init_engine()`.                                                                                                                                                                                                                                                              | Before the warming block: `engine = init_engine()` (or `engine = app.state.db_engine` if init has already run). Same change wherever the plan's lifespan code reads `engine`.                                                                                                                                                                                |
| 6   | Test fixtures (Tasks 1.1, 1.2, 1.3, 2.3, 2.4, 3.x, 4.x)                       | Fixtures `test_engine` / `async_engine` / `sync_engine` do NOT exist in `scripts/tests/conftest.py`. Available: `pg_test_engine` (sync) in `scripts/tests/conftest.py:126` and `engine` (async) in `src/xenon/db/tests/conftest.py:64` (different test root).                                                                                                                                              | Add two fixture helpers to `scripts/tests/conftest.py`: `@pytest.fixture def sync_engine(pg_test_engine): return pg_test_engine` and `@pytest_asyncio.fixture async def async_engine(): from xenon.db.engine import get_engine; init_engine(); yield get_engine()`. Update all new test files.                                                               |
| 7   | All async tests (no marker)                                                   | `pyproject.toml` sets `asyncio_mode = "strict"`. Tests without `@pytest.mark.asyncio` or module-level `pytestmark = pytest.mark.asyncio` will be collected as sync and silently no-op (or hard-fail in pytest-asyncio ≥ 0.24).                                                                                                                                                                             | Add `pytestmark = pytest.mark.asyncio` at the top of every new test file that contains `async def test_*`.                                                                                                                                                                                                                                                   |
| 8   | Task 2.1 Step 1 — `patch("xenon.clients.futu_client.OpenSecTradeContext")`    | `OpenSecTradeContext` is imported INSIDE `connect()` body, not at module scope. `patch()` will raise `AttributeError` at the `with patch(...)` line.                                                                                                                                                                                                                                                       | Refactor `futu_client.py` to import `OpenSecTradeContext` and `TrdEnv` at module level (top of file). OR patch `futu.OpenSecTradeContext` directly. Recommended: module-level import + module-level patch target.                                                                                                                                            |
| 9   | Task 3.2 `fetch_and_cache_benchmark` IB-pool surface                          | The plan uses `ib_pool.with_role("data")` and `ib_pool.contract_for(symbol)` — neither method exists. Actual `IBPool.get(role: str)` returns an `Optional[IBClient]`.                                                                                                                                                                                                                                      | Rewrite: `client = ib_pool.get("data"); if client is None: raise BenchmarkUnavailable("no data role"); from ib_async import Stock; contract = Stock(symbol, "SMART", "USD"); ib = client.ib; bars = ib.reqHistoricalData(contract=contract, ...)`. Also wrap in `client.lock` if `IBClient` exposes one (verify).                                            |
| 10  | Task 4.2 `_atomic_save(result)` call landmark                                 | Actual call in `server.py:2666` is `_atomic_save(str(DATA_DIR / "futu_portfolio.json"), result)` — synchronous, 2-arg. Plan's "after the existing `await _atomic_save(result)`" misnames the call.                                                                                                                                                                                                         | Search for `_atomic_save(` in `server.py`; insert the new persist block AFTER that line. Drop the `await`.                                                                                                                                                                                                                                                   |
| 11  | Task 4.3 Step 2 test — `fake_client.fetch_positions.return_value`             | The FastAPI handler at `server.py:2651` calls `client.fetch_portfolio(force=True)`. If the CLI uses the same client method, the mock target is wrong and the payload never reaches `persist_futu_nav`.                                                                                                                                                                                                     | Grep the CLI source for the actual method (`fetch_positions` or `fetch_portfolio`), then mock that name in the test.                                                                                                                                                                                                                                         |
| 12  | Task 3.3 `_build_series` daily_return formula                                 | Service code says `"daily_return": (float(row["daily_pnl"]) / float(row["nav"]))` — divides by CURRENT nav. Returns array elsewhere divides by PREV nav. The two formulas disagree. Test `test_IB_returns_use_daily_pnl_over_prev_nav` (expecting 0.05) would compute 0.025 and FAIL.                                                                                                                      | Pass the already-computed `returns` array into `_build_series(curve, bench_df, returns)` and serialize `returns[i]` for `daily_return`. (Codex-6.)                                                                                                                                                                                                           |
| 13  | Task 3.3 — first IB return contaminates metrics                               | `returns[0] = daily_pnl[0]/nav[0]` is not a real return (no prior NAV). Pollutes Sharpe/Sortino/vol/distribution.                                                                                                                                                                                                                                                                                          | Set `returns[0] = 0.0` (or `np.nan` + dropna). Add a test where day-1 `daily_pnl ≠ 0`. (Codex-5.)                                                                                                                                                                                                                                                            |
| 14  | Task 3.3 benchmark alignment                                                  | `m = min(len(returns), len(bench_returns)); beta_alpha(returns[-m:], bench[-m:])` aligns by array tail, not by date. Mid-window SPY holes silently shift the join.                                                                                                                                                                                                                                         | Build a date-indexed `pd.DataFrame` of `nav_return` + `bench_return`, drop rows where bench_return is NaN, compute benchmark-relative metrics on the joined frame. (Codex-7.)                                                                                                                                                                                |
| 15  | Task 3.3 fields declared null then never populated                            | `_fill_annualized()`/`_fill_distribution()` set Sharpe/Sortino/var_cvar/beta/alpha/IR/capture and positive/negative/flat/best/worst/hit_rate — but NOT `calmar_ratio`, `tail_ratio`, `ulcer_index`, `correlation`, `r_squared`, `treynor_ratio`, `average_up_day`, `average_down_day`, `win_loss_ratio`, `skew`, `kurtosis`. IB n≥30 will render `---` for these despite the spec's "full panel" contract. | Either implement the missing fillers (call `M.tail_ratio`, `M.ulcer`, scipy `skew`/`kurtosis`, etc.) or remove the fields from `PerformanceSummary` and the test expectations. Add an IB-n=40 test asserting every non-benchmark annualized + distribution field is non-null. (Codex-8.)                                                                     |
| 16  | Task 4.1 `_futu_cached_hero` reads JSON in `src/xenon/api/`                   | CI guard `scripts/checks/no_json_fallback_on_order_path.py` scans `src/xenon/api/` for `json.load` against `data/*.json`. Performance route falls in scope and will fail CI.                                                                                                                                                                                                                               | Add `src/xenon/api/routes/performance.py::_futu_cached_hero` to the `_ALLOWLIST` in the guard. Document the reason: "performance hero is not an order-path read; surfaces the last-known FUTU NAV when no `nav_history` rows exist."                                                                                                                         |
| 17  | Task 5.4 PerformancePanel snippet — duplicate / wrong `data-testid` attribute | Snippet uses `data-testid="low-confidence-badge"` AND a non-standard `data-testid-tip="sharpe-tooltip"` on the same `<Badge>`. Test `screen.getByTestId("sharpe-tooltip")` throws "unable to find element".                                                                                                                                                                                                | Wrap the tooltip in its own element: `<span data-testid="sharpe-tooltip" title={...}>...</span>` next to the badge. Drop `data-testid-tip`.                                                                                                                                                                                                                  |

### High — likely to bite but worked around easily

| #   | Site                                                                         | Defect / risk                                                                                                                                                                                                                                                                                                                                                             | Mitigation                                                                                                                                                                                                                                                                    |
| --- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 18  | Spec §10 `env_from_trd_env` convention vs. existing `account_scope.for_futu` | `account_scope.py:50` already implements `for_futu(account)` using `MODE` (i.e. `XENON_TRADING_MODE`). The plan's `env_from_trd_env("SIMULATE")→"sim"` introduces a SECOND convention. Same FUTU account synced under `XENON_TRADING_MODE=paper` with `trd_env=SIMULATE` produces a `"paper"` row via the old path and a `"sim"` row via the new path → unique-index 409. | Pick one convention: change spec/plan to map `SIMULATE→"paper"` (consistent with IB), OR delete `for_futu(MODE)` and migrate any existing FUTU rows. **Recommended**: map `SIMULATE→"paper"`. Update `env_from_trd_env`, all test fixtures, and the lifespan-warming env_map. |
| 19  | Task 2.3 `persist_futu_nav` cross-env race                                   | App-level read-check then insert is non-atomic. Concurrent writers with different envs can both pass the check; the loser hits the DB unique index as raw `IntegrityError`, surfaced as 500 not 409.                                                                                                                                                                      | Wrap the insert in `try/except sa.exc.IntegrityError`. On catch, re-query `(broker, broker_account, today)` and raise `NavAccountEnvConflict` if env differs. Add a 2-task asyncio test that races two persists.                                                              |
| 20  | Task 4.2 lifespan vs `_trading_mode_paper_default` fixture                   | `scripts/tests/conftest.py:171` mutates `server.app.state.account` AFTER lifespan runs in TestClient. State pollution across tests because `app` is a process-singleton.                                                                                                                                                                                                  | Make the FUTU warming tests use `monkeypatch` to wipe `app.state.futu_account` between tests, or restructure to use a fresh-app fixture.                                                                                                                                      |
| 21  | Task 3.3 / Codex-9 partial benchmark cache                                   | `load_benchmark_cached` only fetches when cache is fully empty. A single stale row + missing rest → no fetch, partial metrics, no warning.                                                                                                                                                                                                                                | Compare cached date set to the requested date range; if any missing → fetch. v1 acceptable if documented.                                                                                                                                                                     |
| 22  | Task 3.2 / Codex-10 IB bar date types                                        | `b.date` from `reqHistoricalData` is sometimes `str` (`"20260601"`) depending on `formatDate`. Comparing `df["date"] >= period_start` against a `date` object will TypeError or silently mis-filter.                                                                                                                                                                      | Normalize via `pd.to_datetime(b.date).date()` before insert. Add a `formatDate=1` test fixture with a string date.                                                                                                                                                            |
| 23  | Task 4.3 CLI async engine leak                                               | `_async_engine_for_cli()` creates a new async engine per invocation and never disposes it. URL normalization only handles `postgresql://`, not `postgresql+psycopg://`.                                                                                                                                                                                                   | Reuse `xenon.db.engine.create_engine()` (or whatever the existing helper is); wrap `asyncio.run` in `try/finally` to `engine.dispose()`.                                                                                                                                      |

### Medium — flag and revisit during execution

| #   | Note                                                                                                                                                                                                                                                                                                                                                                         |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 24  | **Phase 0 verification fragility** — IBKR paper accounts may not reliably reflect deposits, $5 MTM threshold may be too tight. Mitigate: use an all-cash paper account (flatten positions first), make deposit large relative to holdings, baseline NLV pre/post-deposit immediately.                                                                                        |
| 25  | **Multi-worker uvicorn** — `perf_cache` is process-local. If the API runs with `workers > 1`, two workers have independent caches; stale reads possible on the cold worker. Document as v1 limitation; v2 follow-up: Redis-backed cache.                                                                                                                                     |
| 26  | **Half-day market sessions** — `_ttl_for_now` uses `60s` until 16:00 ET, but the market closes at 13:00 ET on some days. Returns 60s TTL during the closed window 13:00–16:00. Acceptable for v1; consider holiday-aware extension later.                                                                                                                                    |
| 27  | **PerformanceSummary nullability expansion** — every consumer of `summary.X` in `PerformancePanel.tsx` and `performanceChart.ts` must handle `null`. Plan Task 5.4 mentions `fmtPct`/`fmtRatio` but not every read site. The executor MUST grep for `summary\.` in those files and gate every read on either `data.status === "ok"` or `value !== null`.                     |
| 28  | **`methodology` / `price_sources` types undefined** — the spec's TypeScript discriminated union references `PerformanceMethodology` and `PerformancePriceSources` interfaces but never defines them. The plan service returns simple dicts (which TypeScript will accept as `any` if the interfaces are loose). Define as `Record<string, unknown>` for v1; structure in v2. |
| 29  | **POST `/performance` subprocess call still alive** — `server.py:2768` actively calls `xenon-portfolio-perf` as a subprocess. The plan's Task 4.1 deprecated POST proxies the new GET, but the executor MUST delete the old subprocess-calling handler in the same PR or it shadows the new one.                                                                             |
| 30  | **`resolve_from_env()` callers outside IB paths** — grep before merging: `grep -rn "resolve_from_env" src/xenon/`. If any caller could pass `XENON_BROKER=FUTU`, the new `ValueError` raise breaks them. Plan currently has no such audit step.                                                                                                                              |

### Resolution policy

When a task instruction conflicts with a correction above:

- **Critical (#1–#17)**: ALWAYS follow the correction.
- **High (#18–#23)**: Follow the correction unless you've personally verified the source and the codebase has changed since 2026-06-01.
- **Medium (#24–#30)**: Implement the correction OR document explicitly why you're deferring it.

A clean run requires all Critical and High corrections applied. Medium corrections may slip to a follow-up PR with explicit tracking in the PR description.

---

---

## File Structure

### Created

| Path                                                                      | Purpose                                                                                                  |
| ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `src/xenon/api/routes/performance.py`                                     | `GET /performance?broker=...` router + `get_performance_scope` dep                                       |
| `src/xenon/api/services/performance.py`                                   | Async `compute(engine, scope)` orchestrator with Phase-0 IB-masking branch + low-confidence              |
| `src/xenon/api/services/futu_nav_persistence.py`                          | `persist_futu_nav` shared helper called from FastAPI and (future) CLI                                    |
| `src/xenon/db/queries/nav_history.py`                                     | `load_nav_curve`, `load_benchmark_cached`, `fetch_and_cache_benchmark`                                   |
| `src/xenon/reports/performance_metrics.py`                                | Pure-math metrics extracted from `portfolio_performance` (no I/O) + Sharpe SE function                   |
| `src/xenon/db/migrations/versions/<rev1>_add_benchmark_closes.py`         | Alembic: `xenon.benchmark_closes (symbol, date, close)`                                                  |
| `src/xenon/db/migrations/versions/<rev2>_add_nav_history_source.py`       | Alembic: `nav_history.source TEXT NOT NULL DEFAULT 'intraday' CHECK (...)`                               |
| `src/xenon/db/migrations/versions/<rev3>_add_nav_history_unique_index.py` | Alembic: `CREATE UNIQUE INDEX nav_history_one_env_per_day ON nav_history (broker, broker_account, date)` |
| `scripts/tests/test_performance_metrics.py`                               | Pure-math fixtures with known Sharpe/Sortino/DD values + `sharpe_se` math                                |
| `scripts/tests/test_performance_service.py`                               | Service-level branch tests (threshold ladder, scope isolation, FUTU cold start, etc.)                    |
| `scripts/tests/test_futu_nav_persistence.py`                              | Persist helper: first insert, same-day update, daily_pnl computed, cross-env raises                      |
| `scripts/tests/test_performance_route.py`                                 | GET/POST endpoint tests (broker query param, deprecated POST stub, 400/200/502)                          |
| `scripts/tests/test_benchmark_cache.py`                                   | Cache miss → IB fetch → upsert; IB error → empty cache + warning string                                  |
| `scripts/tests/test_futu_persist_guard.py`                                | `_acc_id is None`, missing `net_liquidation`, cross-env race → 409                                       |
| `scripts/tests/test_futu_account_warming.py`                              | Lifespan warming from latest FUTU nav row; unknown `account_env` value skipped                           |
| `scripts/tests/test_futu_client_matched_trd_env.py`                       | `FutuClient._matched_trd_env` ground-truth fix + reconnect behaviour                                     |
| `scripts/tests/test_performance_low_confidence.py`                        | `summary.low_confidence` + `sharpe_se`/`sortino_se` ladder at n=30/60/126/200                            |
| `scripts/tests/test_ib_dailypnl_assumption.py`                            | `XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS` env-gate masks IB metrics when true                               |
| `web/tests/usePerformance.test.ts`                                        | Hook re-fetches on `activeAccount` change; broker in cache key                                           |
| `web/tests/PerformancePanel.test.tsx`                                     | Branch rendering for every `status`/`reason`; currency in hero; low-confidence badge + tooltip           |
| `web/e2e/performance-broker-switch.spec.ts`                               | E2E: IB ↔ Futu tab switch produces different hero numbers                                                |
| `web/e2e/performance-futu-cold-start.spec.ts`                             | E2E: fresh boot, FUTU tab shows sync CTA; clicking it unlocks curve                                      |

### Modified

| Path                                                | Phase | Why                                                                                                                                                                                                                                                                                                                                                            |
| --------------------------------------------------- | ----- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | -------------------------- | ---------------------- |
| `src/xenon/db/schema.py`                            | 1     | Add `benchmark_closes` Table; add `source` Column on `nav_history`; reflect the new unique index                                                                                                                                                                                                                                                               |
| `src/xenon/clients/futu_client.py`                  | 2     | Add `_matched_trd_env: Optional[str]` attribute; set in BOTH match and connect-time-fallback paths of `connect()`; clear in `disconnect()`; add public `trd_env_of_matched_account() -> Optional[str]` accessor. Fixes pre-existing silent-env-lie bug at line 142.                                                                                            |
| `src/xenon/execution/account_scope.py`              | 2     | `resolve_from_env()` raises if `XENON_BROKER=FUTU` (FUTU scope only buildable via `persist_futu_nav` / `get_performance_scope`). Add module-level `env_from_trd_env(trd_env: str) -> str` helper mapping `"REAL"→"live"`, `"SIMULATE"→"sim"`, raising on anything else.                                                                                        |
| `src/xenon/execution/ib_sync.py`                    | 2     | `_append_nav_snapshot` reads existing-row `account_env` for `(broker, broker_account, date)` and raises `NavAccountEnvConflict` on mismatch (symmetry with `persist_futu_nav`).                                                                                                                                                                                |
| `src/xenon/api/server.py`                           | 4     | `POST /futu/sync` handler grows `request: Request` param + persist + app.state writes. Lifespan startup warms `app.state.futu_account` + `app.state.futu_trd_env` from latest FUTU `nav_history` row. Deprecated `POST /performance` and `POST /performance/background` default `broker=IB` and proxy the GET (background keeps 202 fire-and-forget contract). |
| `src/xenon/api/guards.py`                           | 4     | Add `get_performance_scope(request)` dep that handles `?broker=IB`, `?broker=FUTU`, missing, invalid, and pre-sync FUTU cases per spec § Empty-state policy.                                                                                                                                                                                                   |
| `src/xenon/reports/portfolio_performance.py`        | 6     | `import` emits `DeprecationWarning`; CLI prints "deprecated, use FastAPI /performance" banner and exits 0                                                                                                                                                                                                                                                      |
| `src/xenon/reports/performance_explainer_report.py` | 6     | Same deprecation treatment                                                                                                                                                                                                                                                                                                                                     |
| `web/lib/types.ts`                                  | 5     | Discriminated union `PerformanceData = PerformanceDataOk                                                                                                                                                                                                                                                                                                       | PerformanceDataInsufficient`; nullable risk-metric fields on `PerformanceSummary`; new `low_confidence: boolean`, `sharpe_se: number | null`, `sortino_se: number | null` per Decisions §4 |
| `web/lib/usePerformance.ts`                         | 5     | Signature `(active, activeAccount)`; endpoint `?broker=${activeAccount.toUpperCase()}`; broker in cache key; `extractTimestamp` union-aware                                                                                                                                                                                                                    |
| `web/lib/performanceChart.ts`                       | 5     | Gate every read of `summary.starting_equity` on `data.status === "ok"`; skip null benchmark points in chart math                                                                                                                                                                                                                                               |
| `web/components/PerformancePanel.tsx`               | 5     | Branch on `data.status` BEFORE destructuring `summary`. Render 4 empty states. Hero shows currency. `fmtPct`/`fmtRatio` accept `null → "---"`. Render `data.warnings`. When `summary.low_confidence === true` show low-confidence badge + SE tooltip on every annualized risk metric.                                                                          |
| `web/components/WorkspaceSections.tsx`              | 5     | Forward `activeAccount` to `PerformancePanel`                                                                                                                                                                                                                                                                                                                  |
| `web/app/api/performance/route.ts`                  | 5     | Reads `broker` from URL search params; forwards via `xenonFetch`. Drops the in-process cache.                                                                                                                                                                                                                                                                  |
| `.env.example`                                      | 0, 3  | Add `XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS` (default `true` — safe-by-default until verified). Add `XENON_PERF_MIN_DAYS_CURVE=5`, `XENON_PERF_MIN_DAYS_METRICS=30`, `XENON_PERF_LOW_CONFIDENCE_DAYS=126`.                                                                                                                                                       |
| `docs/reference/order-path-incident-history.md`     | 7     | Append a row only if implementation surfaces an incident pattern (most likely: no append — performance is not order-path)                                                                                                                                                                                                                                      |

### Deleted

| Path                                          | Why                                              |
| --------------------------------------------- | ------------------------------------------------ |
| `scripts/tests/test_portfolio_performance.py` | Tests retired implementation                     |
| `scripts/tests/test_performance_lock.py`      | Tests POST-dedup behaviour that no longer exists |

---

## Phase 0 — Verification gate (IB `dailyPnL` cash-flow semantics)

**Spec §:** Phase 0 — Verification gate.

This phase is **gating**: no Phase-3+ code that computes IB annualized risk metrics may merge until Task 0.1 completes with a documented outcome. Phases 1 and 2 are unblocked (schema + persist helpers do not depend on the semantics question).

### Task 0.1: Verify IB `dailyPnL` cash-flow semantics empirically

**Files:**

- Create: `docs/superpowers/reports/2026-06-01-ib-dailypnl-verification.md` (verification artifact)
- Modify: `src/xenon/execution/ib_sync.py` (add citation comment to `get_pnl`)
- Modify: `.env.example` (set `XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS` default based on outcome)

- [ ] **Step 1: Try the source check first**

Run: `grep -rn "dailyPnL\|reqPnL" .venv/lib/python3.13/site-packages/ib_async/ 2>/dev/null | head -40`

If the ib_async source includes a docstring or comment specifying cash-flow inclusion/exclusion, capture it as the verification artifact (Step 4) and skip to Step 5.

- [ ] **Step 2: If source is silent, run the paper-account empirical test**

Pre-conditions: IB Gateway running in paper mode (`scripts/infra/dev.sh paper`), Postgres reachable, market closed or steady-state.

On day T-1 at end of session:

```bash
# Record baseline NLV
uv run xenon-ib-sync
psql -h 192.168.50.47 -U xenon_app core_dev -c \
  "SELECT date, nav, daily_pnl FROM xenon.nav_history WHERE broker='IB' AND account_env='sim' ORDER BY date DESC LIMIT 3;"
```

On day T at market open, in TWS paper account: Account → Funding → Deposit Funds → `$1000.00` (simulated). Confirm balance change in TWS.

Throughout day T: do NOT place any trades. Let mark-to-market move organically.

At day T end of session:

```bash
uv run xenon-ib-sync
psql -h 192.168.50.47 -U xenon_app core_dev -c \
  "SELECT date, nav, daily_pnl FROM xenon.nav_history WHERE broker='IB' AND account_env='sim' ORDER BY date DESC LIMIT 3;"
```

- [ ] **Step 3: Interpret the result**

Let `nlv_delta = NLV[T] - NLV[T-1]` and `recorded_dpnl = daily_pnl[T]` (from `nav_history`, which was set from `pnl.dailyPnL`).

| Observation                                                   | Conclusion                                           |
| ------------------------------------------------------------- | ---------------------------------------------------- |
| `recorded_dpnl ≈ nlv_delta` (within ~$5 mark-to-market noise) | `dailyPnL` **INCLUDES** cash flows → outcome (B)     |
| `recorded_dpnl ≈ nlv_delta - 1000` (within ~$5 noise)         | `dailyPnL` **EXCLUDES** cash flows → outcome (A)     |
| Neither holds                                                 | Inconclusive → outcome (C) (default to B for safety) |

- [ ] **Step 4: Write the verification artifact**

```bash
cat > docs/superpowers/reports/2026-06-01-ib-dailypnl-verification.md <<'EOF'
# IB dailyPnL cash-flow semantics — verification artifact

**Date:** 2026-06-01
**Account:** paper / sim
**Method:** Source check (Step 1) + paper deposit empirical test (Steps 2–3)

## Raw numbers

| Day | NLV (USD) | nav_history.daily_pnl (USD) | Notes |
| --- | --------- | --------------------------- | ----- |
| T-1 | ... | ... | baseline |
| T   | ... | ... | $1000 deposit at open, no trades |

NLV delta day T − day T-1: ...
Recorded daily_pnl day T: ...

## Conclusion

[ ] (A) dailyPnL EXCLUDES cash flows — proceed with v1 spec
[ ] (B) dailyPnL INCLUDES cash flows — mask IB risk metrics until cash-flow tracking lands
[ ] (C) Inconclusive — default to (B)

Outcome: ___
EOF
```

Fill in the table from Step 3. Commit.

- [ ] **Step 5: Update `ib_sync.py` and `.env.example`**

In `src/xenon/execution/ib_sync.py::get_pnl`, add this comment immediately above the `pnl = client.get_pnl(account)` line:

```python
# pnl.dailyPnL semantics — verified 2026-06-01 (see
# docs/superpowers/reports/2026-06-01-ib-dailypnl-verification.md):
#   outcome (A): excludes cash flows — safe to use for return computation
#   outcome (B): includes cash flows — IB risk metrics must mask same as FUTU
# The XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS env var (read in performance_service)
# selects the behavior. Default is `true` (the safe-but-pessimistic case).
```

In `.env.example` add:

```bash
# Set to "false" only if Phase 0 verification confirmed dailyPnL excludes cash flows.
# When "true" (default), IB risk metrics are masked the same as FUTU until cash-flow
# tracking lands. See docs/superpowers/reports/2026-06-01-ib-dailypnl-verification.md.
XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS=true
```

- [ ] **Step 6: Commit**

```bash
git add docs/superpowers/reports/2026-06-01-ib-dailypnl-verification.md \
        src/xenon/execution/ib_sync.py .env.example
git commit -m "verify(perf): document IB dailyPnL cash-flow semantics"
```

---

## Phase 1 — Schema

### Task 1.1: Add `benchmark_closes` table

**Files:**

- Create: `src/xenon/db/migrations/versions/<rev>_add_benchmark_closes.py`
- Modify: `src/xenon/db/schema.py` (add Table object)
- Test: `scripts/tests/test_schema_benchmark_closes.py`

- [ ] **Step 1: Generate migration skeleton**

Run: `uv run alembic revision -m "add benchmark_closes table"`

This creates a file like `src/xenon/db/migrations/versions/abc123_add_benchmark_closes.py`. Note its revision id.

- [ ] **Step 2: Fill in the migration body**

Replace the autogenerated body with:

```python
"""add benchmark_closes table

Revision ID: <auto>
Revises: <prev>
"""
from alembic import op
import sqlalchemy as sa

revision = "<auto>"
down_revision = "<prev>"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "benchmark_closes",
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("close", sa.Numeric(14, 4), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "date"),
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_table("benchmark_closes", schema="xenon")
```

- [ ] **Step 3: Add the SQLAlchemy reflection in `schema.py`**

In `src/xenon/db/schema.py`, find the `metadata` definition and add (alphabetically with other xenon tables):

```python
benchmark_closes = sa.Table(
    "benchmark_closes",
    metadata,
    sa.Column("symbol", sa.Text, primary_key=True),
    sa.Column("date", sa.Date, primary_key=True),
    sa.Column("close", sa.Numeric(14, 4), nullable=False),
    schema="xenon",
)
```

- [ ] **Step 4: Write the schema test**

`scripts/tests/test_schema_benchmark_closes.py`:

```python
import sqlalchemy as sa
from xenon.db.schema import benchmark_closes


def test_benchmark_closes_can_insert_and_read(test_engine):
    with test_engine.begin() as conn:
        conn.execute(
            sa.insert(benchmark_closes).values(symbol="SPY", date="2026-06-01", close="450.00")
        )
        row = conn.execute(sa.select(benchmark_closes).where(benchmark_closes.c.symbol == "SPY")).first()
        assert row is not None
        assert float(row.close) == 450.00
```

- [ ] **Step 5: Apply the migration and run the test**

Run: `uv run alembic upgrade head && uv run pytest scripts/tests/test_schema_benchmark_closes.py -xvs`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/db/migrations/versions/<rev>_add_benchmark_closes.py \
        src/xenon/db/schema.py scripts/tests/test_schema_benchmark_closes.py
git commit -m "feat(db): add xenon.benchmark_closes table"
```

### Task 1.2: Add `nav_history.source` column

**Files:**

- Create: `src/xenon/db/migrations/versions/<rev>_add_nav_history_source.py`
- Modify: `src/xenon/db/schema.py` (add Column)
- Test: `scripts/tests/test_schema_nav_history_source.py`

- [ ] **Step 1: Generate migration**

Run: `uv run alembic revision -m "add nav_history.source column"`

- [ ] **Step 2: Fill in the body**

```python
def upgrade() -> None:
    op.add_column(
        "nav_history",
        sa.Column("source", sa.Text(), nullable=False, server_default="intraday"),
        schema="xenon",
    )
    op.create_check_constraint(
        "ck_nav_history_source",
        "nav_history",
        "source IN ('close', 'intraday')",
        schema="xenon",
    )


def downgrade() -> None:
    op.drop_constraint("ck_nav_history_source", "nav_history", schema="xenon")
    op.drop_column("nav_history", "source", schema="xenon")
```

- [ ] **Step 3: Add the Column to `schema.py`**

In `src/xenon/db/schema.py`, find the existing `nav_history = sa.Table(...)` definition and append a column after the existing ones:

```python
    sa.Column("source", sa.Text, nullable=False, server_default="intraday"),
```

- [ ] **Step 4: Test the check constraint**

`scripts/tests/test_schema_nav_history_source.py`:

```python
import sqlalchemy as sa
import pytest
from xenon.db.schema import nav_history


def test_nav_history_source_defaults_to_intraday(test_engine):
    with test_engine.begin() as conn:
        conn.execute(
            sa.insert(nav_history).values(
                broker="IB", account_env="sim", broker_account="DU123",
                date="2026-06-01", nav="50000.00", daily_pnl="0.00",
            )
        )
        row = conn.execute(
            sa.select(nav_history.c.source).where(nav_history.c.broker == "IB")
        ).first()
        assert row.source == "intraday"


def test_nav_history_source_rejects_unknown_value(test_engine):
    with test_engine.begin() as conn:
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(
                sa.insert(nav_history).values(
                    broker="IB", account_env="sim", broker_account="DU123",
                    date="2026-06-02", nav="50000.00", daily_pnl="0.00",
                    source="bogus",
                )
            )
```

- [ ] **Step 5: Apply + run test**

Run: `uv run alembic upgrade head && uv run pytest scripts/tests/test_schema_nav_history_source.py -xvs`

Expected: both PASS.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/db/migrations/versions/<rev>_add_nav_history_source.py \
        src/xenon/db/schema.py scripts/tests/test_schema_nav_history_source.py
git commit -m "feat(db): add nav_history.source column with check"
```

### Task 1.3: Add `nav_history_one_env_per_day` partial unique index

**Files:**

- Create: `src/xenon/db/migrations/versions/<rev>_add_nav_history_unique_index.py`
- Test: `scripts/tests/test_schema_nav_history_unique_index.py`

- [ ] **Step 1: Generate migration**

Run: `uv run alembic revision -m "add nav_history_one_env_per_day unique index"`

- [ ] **Step 2: Fill in the body**

```python
def upgrade() -> None:
    op.execute("""
        CREATE UNIQUE INDEX nav_history_one_env_per_day
        ON xenon.nav_history (broker, broker_account, date)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS xenon.nav_history_one_env_per_day")
```

- [ ] **Step 3: Write the regression test (this is the spec's load-bearing race protection)**

`scripts/tests/test_schema_nav_history_unique_index.py`:

```python
import sqlalchemy as sa
import pytest
from xenon.db.schema import nav_history


def test_two_different_account_envs_for_same_account_date_rejected(test_engine):
    with test_engine.begin() as conn:
        conn.execute(sa.insert(nav_history).values(
            broker="FUTU", account_env="live", broker_account="42",
            date="2026-06-01", nav="100000.00", daily_pnl="0.00",
        ))
    # Different account_env, same (broker, broker_account, date) → must fail
    with pytest.raises(sa.exc.IntegrityError):
        with test_engine.begin() as conn:
            conn.execute(sa.insert(nav_history).values(
                broker="FUTU", account_env="sim", broker_account="42",
                date="2026-06-01", nav="100000.00", daily_pnl="0.00",
            ))


def test_same_account_env_for_same_account_date_still_PK_blocked(test_engine):
    # Existing PK already covers this; sanity check the unique index doesn't
    # accidentally relax it.
    with test_engine.begin() as conn:
        conn.execute(sa.insert(nav_history).values(
            broker="FUTU", account_env="live", broker_account="43",
            date="2026-06-01", nav="100000.00", daily_pnl="0.00",
        ))
    with pytest.raises(sa.exc.IntegrityError):
        with test_engine.begin() as conn:
            conn.execute(sa.insert(nav_history).values(
                broker="FUTU", account_env="live", broker_account="43",
                date="2026-06-01", nav="200000.00", daily_pnl="0.00",
            ))
```

- [ ] **Step 4: Apply + run**

Run: `uv run alembic upgrade head && uv run pytest scripts/tests/test_schema_nav_history_unique_index.py -xvs`

Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/db/migrations/versions/<rev>_add_nav_history_unique_index.py \
        scripts/tests/test_schema_nav_history_unique_index.py
git commit -m "feat(db): add nav_history_one_env_per_day unique index"
```

---

## Phase 2 — Backend foundations (no UI yet)

### Task 2.1: Fix `FutuClient` silent-env-lie + add `_matched_trd_env`

**Spec §:** Decisions §10.

**Files:**

- Modify: `src/xenon/clients/futu_client.py:60-180` (attribute, connect, disconnect, accessor)
- Test: `scripts/tests/test_futu_client_matched_trd_env.py`

- [ ] **Step 1: Write the failing tests**

`scripts/tests/test_futu_client_matched_trd_env.py`:

```python
"""Validates Decisions §10 fix: ground-truth trd_env via _matched_trd_env.

Pre-existing bug: FutuClient.connect() at line 142 falls back to the first
account when the requested trd_env has no match, but never updates self.trd_env.
So self.trd_env reports "REAL" while connected to a SIMULATE account. The
new _matched_trd_env attribute is the ground truth.
"""
import pandas as pd
import pytest
from unittest.mock import MagicMock, patch
from xenon.clients.futu_client import FutuClient


@pytest.fixture
def mock_futu_ctx():
    """Returns (OpenSecTradeContext mock, get_acc_list mock data setter)."""
    with patch("xenon.clients.futu_client.OpenSecTradeContext") as ctx_cls:
        ctx = MagicMock()
        ctx_cls.return_value = ctx
        # Default to RET_OK so connect() does not raise unless a test overrides it.
        from futu import RET_OK
        ctx.get_acc_list.return_value = (RET_OK, pd.DataFrame())
        yield ctx


def _acc_list_df(rows):
    """rows: list of (acc_id, trd_env_enum_value)."""
    from futu import TrdEnv
    enum_map = {"REAL": TrdEnv.REAL, "SIMULATE": TrdEnv.SIMULATE}
    return pd.DataFrame([{"acc_id": acc_id, "trd_env": enum_map[env]} for acc_id, env in rows])


def test_matched_trd_env_is_REAL_when_real_account_present(mock_futu_ctx):
    from futu import RET_OK
    mock_futu_ctx.get_acc_list.return_value = (RET_OK, _acc_list_df([(100, "REAL"), (200, "SIMULATE")]))
    client = FutuClient(trd_env="REAL")
    client.connect()
    assert client.trd_env_of_matched_account() == "REAL"
    assert client._acc_id == 100


def test_matched_trd_env_is_SIMULATE_when_fallback_path_hits(mock_futu_ctx):
    """REGRESSION: pre-existing bug — fallback selects first row, self.trd_env was never updated.
    The new attribute must report the matched row's actual env, not the requested one."""
    from futu import RET_OK
    mock_futu_ctx.get_acc_list.return_value = (RET_OK, _acc_list_df([(300, "SIMULATE")]))
    client = FutuClient(trd_env="REAL")  # requested REAL, but only SIMULATE present
    client.connect()
    assert client.trd_env_of_matched_account() == "SIMULATE"  # ground truth
    assert client.trd_env == "REAL"  # legacy field unchanged — for logging only
    assert client._acc_id == 300


def test_matched_trd_env_clears_on_disconnect(mock_futu_ctx):
    from futu import RET_OK
    mock_futu_ctx.get_acc_list.return_value = (RET_OK, _acc_list_df([(100, "REAL")]))
    client = FutuClient(trd_env="REAL")
    client.connect()
    assert client.trd_env_of_matched_account() == "REAL"
    client.disconnect()
    assert client.trd_env_of_matched_account() is None


def test_matched_trd_env_repopulates_after_reconnect(mock_futu_ctx):
    from futu import RET_OK
    mock_futu_ctx.get_acc_list.return_value = (RET_OK, _acc_list_df([(100, "SIMULATE")]))
    client = FutuClient(trd_env="REAL")
    client.connect()
    assert client.trd_env_of_matched_account() == "SIMULATE"
    client.disconnect()
    mock_futu_ctx.get_acc_list.return_value = (RET_OK, _acc_list_df([(200, "REAL")]))
    client.connect()
    assert client.trd_env_of_matched_account() == "REAL"
    assert client._acc_id == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest scripts/tests/test_futu_client_matched_trd_env.py -xvs`

Expected: FAIL with `AttributeError: 'FutuClient' object has no attribute 'trd_env_of_matched_account'`.

- [ ] **Step 3: Apply the attribute + accessor + connect/disconnect logic**

Edit `src/xenon/clients/futu_client.py`:

In `__init__` (after `self._acc_id: Optional[int] = None`), add:

```python
        # Decisions §10: ground-truth env of the actually-matched account row.
        # self.trd_env above is the *requested* env (logging only). This attribute
        # is the *matched row's* env and is the single source of truth for any
        # nav_history / scope persistence. None when not connected.
        self._matched_trd_env: Optional[str] = None
```

In `connect()`, replace the matching block (currently at ~lines 139–149) with:

```python
            env_enum = getattr(TrdEnv, self.trd_env, TrdEnv.REAL)
            matching = data[data["trd_env"] == env_enum]
            if matching.empty:
                # Fallback: no account in requested env. Pick first row AND record
                # the actual env so callers don't get a silent lie.
                self._acc_id = int(data["acc_id"].iloc[0])
                self._matched_trd_env = _enum_to_str(data["trd_env"].iloc[0])
                logger.warning(
                    "No %s account on OpenD, falling back to first acc_id=%s env=%s",
                    self.trd_env, self._acc_id, self._matched_trd_env,
                )
            else:
                self._acc_id = int(matching["acc_id"].iloc[0])
                self._matched_trd_env = _enum_to_str(matching["trd_env"].iloc[0])
```

In `disconnect()`, after `self._acc_id = None`, add:

```python
        self._matched_trd_env = None
```

After the class definition (or near the helper imports at top of file), add:

```python
def _enum_to_str(value: Any) -> str:
    """Map a futu TrdEnv enum value back to its name (REAL / SIMULATE)."""
    from futu import TrdEnv
    if value == TrdEnv.REAL:
        return "REAL"
    if value == TrdEnv.SIMULATE:
        return "SIMULATE"
    raise ValueError(f"Unknown TrdEnv value: {value!r}")
```

Add the public accessor as a method on `FutuClient` (place it near `is_connected`):

```python
    def trd_env_of_matched_account(self) -> Optional[str]:
        """Ground-truth env of the matched OpenD account. None when not connected.

        Always prefer this over self.trd_env (which is only the requested value
        and may not match after a connect-time fallback).
        """
        return self._matched_trd_env
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest scripts/tests/test_futu_client_matched_trd_env.py -xvs`

Expected: 4 PASS.

- [ ] **Step 5: Run the existing futu_client tests to verify no regression**

Run: `uv run pytest scripts/tests/ -k futu -xvs`

Expected: all existing futu tests still PASS.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/clients/futu_client.py scripts/tests/test_futu_client_matched_trd_env.py
git commit -m "fix(futu): expose matched-account trd_env, fix silent-env-lie at connect fallback"
```

### Task 2.2: `account_scope.resolve_from_env` rejects FUTU + `env_from_trd_env` helper

**Spec §:** Decisions §10.

**Files:**

- Modify: `src/xenon/execution/account_scope.py`
- Test: `scripts/tests/test_account_scope_env_from_trd_env.py`

- [ ] **Step 1: Write the failing test**

`scripts/tests/test_account_scope_env_from_trd_env.py`:

```python
import os
import pytest
from xenon.execution.account_scope import (
    AccountScope, env_from_trd_env, resolve_from_env,
)


def test_env_from_trd_env_maps_REAL_to_live():
    assert env_from_trd_env("REAL") == "live"


def test_env_from_trd_env_maps_SIMULATE_to_sim():
    assert env_from_trd_env("SIMULATE") == "sim"


def test_env_from_trd_env_rejects_unknown():
    with pytest.raises(ValueError):
        env_from_trd_env("XYZ")


def test_resolve_from_env_rejects_FUTU(monkeypatch):
    monkeypatch.setenv("XENON_BROKER", "FUTU")
    monkeypatch.setenv("XENON_TRADING_MODE", "live")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "42")
    with pytest.raises(ValueError, match="FUTU"):
        resolve_from_env()


def test_resolve_from_env_still_works_for_IB(monkeypatch):
    monkeypatch.setenv("XENON_BROKER", "IB")
    monkeypatch.setenv("XENON_TRADING_MODE", "sim")
    monkeypatch.setenv("XENON_BROKER_ACCOUNT", "DU123")
    scope = resolve_from_env()
    assert scope.broker == "IB"
    assert scope.account_env == "sim"
    assert scope.broker_account == "DU123"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_account_scope_env_from_trd_env.py -xvs`

Expected: FAIL with ImportError on `env_from_trd_env`.

- [ ] **Step 3: Implement**

In `src/xenon/execution/account_scope.py`, add a module-level function:

```python
def env_from_trd_env(trd_env: str) -> str:
    """Map FutuClient's matched trd_env string to xenon's account_env value.

    REAL → "live", SIMULATE → "sim". Raises on any other input — the caller
    must validate before calling (e.g. via FutuClient.trd_env_of_matched_account()).
    """
    if trd_env == "REAL":
        return "live"
    if trd_env == "SIMULATE":
        return "sim"
    raise ValueError(f"Unknown Futu trd_env: {trd_env!r}")
```

In `resolve_from_env`, after parsing `XENON_BROKER`, add early:

```python
    if broker == "FUTU":
        raise ValueError(
            "FUTU scope cannot be resolved from env vars — "
            "use persist_futu_nav or get_performance_scope. See Decisions §10."
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest scripts/tests/test_account_scope_env_from_trd_env.py -xvs`

Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/account_scope.py scripts/tests/test_account_scope_env_from_trd_env.py
git commit -m "feat(scope): env_from_trd_env helper; reject FUTU in resolve_from_env"
```

### Task 2.3: `persist_futu_nav` helper

**Spec §:** Persistence flow (FUTU NAV).

**Files:**

- Create: `src/xenon/api/services/futu_nav_persistence.py`
- Test: `scripts/tests/test_futu_nav_persistence.py`, `scripts/tests/test_futu_persist_guard.py`

- [ ] **Step 1: Write the failing tests**

`scripts/tests/test_futu_nav_persistence.py`:

```python
import sqlalchemy as sa
import pytest
from datetime import date
from unittest.mock import MagicMock
from xenon.api.services.futu_nav_persistence import (
    persist_futu_nav, NavAccountEnvConflict,
)
from xenon.db.schema import nav_history


@pytest.fixture
def fake_client():
    c = MagicMock()
    c._acc_id = 42
    return c


@pytest.fixture
def payload():
    return {"account_summary": {"net_liquidation": 100000.00}}


async def test_first_call_inserts_FUTU_live_row(async_engine, fake_client, payload):
    await persist_futu_nav(async_engine, fake_client, "REAL", payload)
    async with async_engine.begin() as conn:
        row = (await conn.execute(
            sa.select(nav_history).where(
                (nav_history.c.broker == "FUTU") & (nav_history.c.broker_account == "42")
            )
        )).first()
    assert row is not None
    assert row.account_env == "live"
    assert float(row.nav) == 100000.00
    assert row.daily_pnl is None  # first day, no prev
    assert row.source == "intraday"


async def test_next_day_computes_daily_pnl_from_prev_row(async_engine, fake_client):
    # Seed yesterday
    async with async_engine.begin() as conn:
        await conn.execute(sa.insert(nav_history).values(
            broker="FUTU", account_env="live", broker_account="42",
            date=date(2026, 5, 30), nav="95000.00", daily_pnl="0.00", source="intraday",
        ))
    # Today
    await persist_futu_nav(async_engine, fake_client, "REAL",
                           {"account_summary": {"net_liquidation": 100000.00}})
    async with async_engine.begin() as conn:
        row = (await conn.execute(
            sa.select(nav_history).where(
                (nav_history.c.broker == "FUTU") & (nav_history.c.date == date.today())
            )
        )).first()
    assert float(row.daily_pnl) == 5000.00


async def test_cross_env_collision_raises(async_engine, fake_client):
    # Seed today as "live"
    async with async_engine.begin() as conn:
        await conn.execute(sa.insert(nav_history).values(
            broker="FUTU", account_env="live", broker_account="42",
            date=date.today(), nav="100000.00", daily_pnl="0.00", source="intraday",
        ))
    # Same broker_account, same date, different env → raise
    with pytest.raises(NavAccountEnvConflict):
        await persist_futu_nav(async_engine, fake_client, "SIMULATE",
                               {"account_summary": {"net_liquidation": 100000.00}})


async def test_ignores_payload_daily_pnl_field(async_engine, fake_client):
    """payload['daily_pnl'] is lifetime unrealized — never trust it."""
    await persist_futu_nav(async_engine, fake_client, "REAL", {
        "account_summary": {"net_liquidation": 100000.00, "daily_pnl": 9999.99},
    })
    async with async_engine.begin() as conn:
        row = (await conn.execute(
            sa.select(nav_history).where(nav_history.c.broker == "FUTU")
        )).first()
    assert row.daily_pnl is None  # no prev row → computed None, NOT 9999.99
```

`scripts/tests/test_futu_persist_guard.py`:

```python
import sqlalchemy as sa
import pytest
from unittest.mock import MagicMock
from xenon.api.services.futu_nav_persistence import persist_futu_nav
from xenon.db.schema import nav_history


async def test_acc_id_None_returns_early_no_row(async_engine, caplog):
    client = MagicMock()
    client._acc_id = None
    await persist_futu_nav(async_engine, client, "REAL",
                           {"account_summary": {"net_liquidation": 100000.00}})
    async with async_engine.begin() as conn:
        count = (await conn.execute(sa.select(sa.func.count()).select_from(nav_history))).scalar()
    assert count == 0
    assert any("_acc_id is None" in r.message for r in caplog.records)


async def test_unknown_matched_trd_env_returns_early_no_row(async_engine, caplog):
    client = MagicMock()
    client._acc_id = 42
    await persist_futu_nav(async_engine, client, "BOGUS",
                           {"account_summary": {"net_liquidation": 100000.00}})
    async with async_engine.begin() as conn:
        count = (await conn.execute(sa.select(sa.func.count()).select_from(nav_history))).scalar()
    assert count == 0
    assert any("unknown matched_trd_env" in r.message for r in caplog.records)


async def test_missing_net_liq_returns_early_no_row(async_engine, caplog):
    client = MagicMock()
    client._acc_id = 42
    await persist_futu_nav(async_engine, client, "REAL", {"account_summary": {}})
    async with async_engine.begin() as conn:
        count = (await conn.execute(sa.select(sa.func.count()).select_from(nav_history))).scalar()
    assert count == 0
    assert any("missing net_liquidation" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest scripts/tests/test_futu_nav_persistence.py scripts/tests/test_futu_persist_guard.py -xvs`

Expected: ImportError on `persist_futu_nav`.

- [ ] **Step 3: Implement `futu_nav_persistence.py`**

```python
"""Shared helper for persisting FUTU NAV from FastAPI and CLI paths.

Implements the persistence flow from
docs/superpowers/specs/2026-05-31-performance-rebuild-design.md
§ Persistence flow (FUTU NAV).
"""
from __future__ import annotations
import logging
from typing import Any
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine
from xenon.db.schema import nav_history
from xenon.execution.account_scope import AccountScope, env_from_trd_env
from xenon.utils.market_calendar import current_session_date_et

logger = logging.getLogger(__name__)


class NavAccountEnvConflict(Exception):
    """Raised when a write would create a (broker, broker_account, date) row
    with an account_env different from the existing row. Mapped to 409 by
    the FastAPI handler."""

    def __init__(self, scope: AccountScope, existing_env: str, date_):
        super().__init__(
            f"NAV account_env conflict: existing={existing_env!r} new={scope.account_env!r} "
            f"for ({scope.broker}, {scope.broker_account}, {date_})"
        )
        self.scope = scope
        self.existing_env = existing_env
        self.date = date_


def _safe_extract_net_liq(payload: dict) -> float | None:
    try:
        v = payload["account_summary"]["net_liquidation"]
    except (KeyError, TypeError):
        return None
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def persist_futu_nav(
    engine: AsyncEngine,
    futu_client: Any,
    matched_trd_env: str,
    payload: dict,
) -> None:
    """Persist a FUTU NAV row scoped to the currently-connected account.

    Early-returns (logs warning, no row written) on:
      - futu_client._acc_id is None (transient OpenD disconnect)
      - matched_trd_env not in {"REAL", "SIMULATE"} (caller didn't resolve via accessor)
      - payload missing or malformed net_liquidation

    Raises NavAccountEnvConflict on cross-env collision (Decisions §13).
    """
    if futu_client._acc_id is None:
        logger.warning("persist_futu_nav skipped: _acc_id is None")
        return
    if matched_trd_env not in {"REAL", "SIMULATE"}:
        logger.warning("persist_futu_nav skipped: unknown matched_trd_env=%r", matched_trd_env)
        return
    net_liq = _safe_extract_net_liq(payload)
    if net_liq is None:
        logger.warning("persist_futu_nav skipped: payload missing net_liquidation")
        return

    scope = AccountScope(
        broker="FUTU",
        account_env=env_from_trd_env(matched_trd_env),
        broker_account=str(futu_client._acc_id),
    )
    today = current_session_date_et()

    async with engine.begin() as conn:
        # App-level guard (defense-in-depth alongside the unique index).
        existing_env_row = (await conn.execute(
            sa.select(nav_history.c.account_env).where(
                (nav_history.c.broker == scope.broker)
                & (nav_history.c.broker_account == scope.broker_account)
                & (nav_history.c.date == today)
            )
        )).first()
        if existing_env_row is not None and existing_env_row.account_env != scope.account_env:
            raise NavAccountEnvConflict(scope, existing_env_row.account_env, today)

        prev_nav_row = (await conn.execute(
            sa.select(nav_history.c.nav).where(
                (nav_history.c.broker == scope.broker)
                & (nav_history.c.account_env == scope.account_env)
                & (nav_history.c.broker_account == scope.broker_account)
                & (nav_history.c.date < today)
            ).order_by(nav_history.c.date.desc()).limit(1)
        )).first()
        prev_nav = float(prev_nav_row.nav) if prev_nav_row else None
        daily_pnl = (net_liq - prev_nav) if prev_nav is not None else None

        stmt = pg_insert(nav_history).values(
            broker=scope.broker,
            account_env=scope.account_env,
            broker_account=scope.broker_account,
            date=today,
            nav=net_liq,
            daily_pnl=daily_pnl,
            source="intraday",
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["broker", "account_env", "broker_account", "date"],
            set_={"nav": net_liq, "daily_pnl": daily_pnl, "source": "intraday"},
        )
        await conn.execute(stmt)
```

- [ ] **Step 4: Run to verify all PASS**

Run: `uv run pytest scripts/tests/test_futu_nav_persistence.py scripts/tests/test_futu_persist_guard.py -xvs`

Expected: 7 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/services/futu_nav_persistence.py \
        scripts/tests/test_futu_nav_persistence.py \
        scripts/tests/test_futu_persist_guard.py
git commit -m "feat(futu): persist_futu_nav helper with guards"
```

### Task 2.4: IB `_append_nav_snapshot` cross-env guard

**Spec §:** Schema changes — second paragraph.

**Files:**

- Modify: `src/xenon/execution/ib_sync.py` (find `_append_nav_snapshot`)
- Test: `scripts/tests/test_ib_sync_cross_env_guard.py`

- [ ] **Step 1: Write the failing test**

`scripts/tests/test_ib_sync_cross_env_guard.py`:

```python
import sqlalchemy as sa
import pytest
from datetime import date
from xenon.execution.ib_sync import _append_nav_snapshot
from xenon.execution.account_scope import AccountScope
from xenon.api.services.futu_nav_persistence import NavAccountEnvConflict
from xenon.db.schema import nav_history


def test_append_raises_on_account_env_mismatch(sync_engine):
    # Seed live row
    with sync_engine.begin() as conn:
        conn.execute(sa.insert(nav_history).values(
            broker="IB", account_env="live", broker_account="U1234567",
            date=date(2026, 6, 1), nav="100000.00", daily_pnl="0.00", source="intraday",
        ))
    sim_scope = AccountScope(broker="IB", account_env="sim", broker_account="U1234567")
    with pytest.raises(NavAccountEnvConflict):
        _append_nav_snapshot(sync_engine, sim_scope, date(2026, 6, 1), 100000.00, 0.00)


def test_append_proceeds_when_env_matches(sync_engine):
    scope = AccountScope(broker="IB", account_env="sim", broker_account="DU9876543")
    _append_nav_snapshot(sync_engine, scope, date(2026, 6, 1), 50000.00, 0.00)
    with sync_engine.begin() as conn:
        row = conn.execute(
            sa.select(nav_history).where(nav_history.c.broker_account == "DU9876543")
        ).first()
    assert float(row.nav) == 50000.00
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_ib_sync_cross_env_guard.py -xvs`

Expected: FAIL (NavAccountEnvConflict not raised).

- [ ] **Step 3: Patch `_append_nav_snapshot`**

In `src/xenon/execution/ib_sync.py`, find `_append_nav_snapshot(engine, scope, date_, nav, daily_pnl)` (or equivalent name) and add this at the start of the function body, before any insert/upsert:

```python
    # Decisions §13 — cross-env collision guard (symmetry with persist_futu_nav).
    from xenon.api.services.futu_nav_persistence import NavAccountEnvConflict
    with engine.begin() as conn:
        existing = conn.execute(
            sa.select(nav_history.c.account_env).where(
                (nav_history.c.broker == scope.broker)
                & (nav_history.c.broker_account == scope.broker_account)
                & (nav_history.c.date == date_)
            )
        ).first()
        if existing is not None and existing.account_env != scope.account_env:
            raise NavAccountEnvConflict(scope, existing.account_env, date_)
```

(Adjust the variable names to match the function's actual signature — the spec calls it `_append_nav_snapshot`; the working tree may use a slightly different parameter list. The point is the read-before-write guard.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest scripts/tests/test_ib_sync_cross_env_guard.py -xvs`

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/execution/ib_sync.py scripts/tests/test_ib_sync_cross_env_guard.py
git commit -m "feat(ib_sync): cross-env conflict guard for nav_history writes"
```

---

## Phase 3 — Service and queries

### Task 3.1: `performance_metrics.py` pure-math extraction + `sharpe_se`

**Spec §:** Architecture > Components > `performance_metrics.py`.

**Files:**

- Create: `src/xenon/reports/performance_metrics.py`
- Test: `scripts/tests/test_performance_metrics.py`

- [ ] **Step 1: Identify functions to extract from `portfolio_performance.py`**

Run: `grep -n "^def " src/xenon/reports/portfolio_performance.py` and list the candidates.

The spec's required exported functions are: `sharpe`, `sortino`, `max_drawdown`, `beta_alpha`, `information_ratio`, `upside_downside_capture`, `var_cvar`, `tail_ratio`, `ulcer`, `skew`, `kurtosis`, `hit_rate`. **Add new**: `sharpe_se(n_sessions, periods=252)` returning the standard error of the Sharpe estimate.

- [ ] **Step 2: Write failing tests**

`scripts/tests/test_performance_metrics.py`:

```python
import numpy as np
import pytest
from xenon.reports.performance_metrics import (
    sharpe, sortino, max_drawdown, beta_alpha, information_ratio,
    upside_downside_capture, var_cvar, sharpe_se,
)


def test_sharpe_known_value():
    # Daily returns with mean 0.001, std 0.01 → Sharpe ≈ 0.1 * sqrt(252) ≈ 1.587
    np.random.seed(0)
    returns = np.full(252, 0.001) + np.random.normal(0, 0.01, 252)
    s = sharpe(returns, rf=0.0, periods=252)
    assert 1.0 < s < 2.5


def test_max_drawdown_simple():
    equity = np.array([100, 110, 105, 95, 100])
    depth, duration, _trough = max_drawdown(equity)
    assert abs(depth - (95 - 110) / 110) < 1e-9  # -13.636%
    assert duration == 2  # 2 sessions from peak to trough


def test_sharpe_se_table():
    """SE ≈ sqrt(periods/n) — verified at canonical n values."""
    assert sharpe_se(30, periods=252) == pytest.approx(np.sqrt(252 / 30), rel=1e-9)
    assert sharpe_se(60, periods=252) == pytest.approx(np.sqrt(252 / 60), rel=1e-9)
    assert sharpe_se(126, periods=252) == pytest.approx(np.sqrt(252 / 126), rel=1e-9)
    assert sharpe_se(252, periods=252) == pytest.approx(1.0, rel=1e-9)


def test_sharpe_se_raises_on_zero_n():
    with pytest.raises(ValueError):
        sharpe_se(0)


def test_var_cvar_negative_tail():
    np.random.seed(0)
    returns = np.random.normal(0, 0.02, 1000)
    v, c = var_cvar(returns, percentile=0.05)
    assert v < 0
    assert c < v  # CVaR is always more negative than VaR
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_performance_metrics.py -xvs`

Expected: ImportError.

- [ ] **Step 3: Create the module by lifting existing math + adding `sharpe_se`**

```python
"""Pure-math performance metrics. No I/O. No async.

Lifted from xenon.reports.portfolio_performance with no semantic change
except for the addition of sharpe_se (per Decisions §4).
"""
from __future__ import annotations
import numpy as np
import pandas as pd


def sharpe(returns: np.ndarray, rf: float = 0.0, periods: int = 252) -> float:
    excess = returns - rf / periods
    sd = float(np.std(excess, ddof=1))
    if sd == 0.0:
        return 0.0
    return float(np.mean(excess) / sd * np.sqrt(periods))


def sortino(returns: np.ndarray, rf: float = 0.0, periods: int = 252) -> float:
    excess = returns - rf / periods
    downside = excess[excess < 0]
    if len(downside) == 0:
        return 0.0
    dd_std = float(np.std(downside, ddof=1))
    if dd_std == 0.0:
        return 0.0
    return float(np.mean(excess) / dd_std * np.sqrt(periods))


def max_drawdown(equity: np.ndarray) -> tuple[float, int, int]:
    """Returns (depth_fraction, duration_sessions, trough_index)."""
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    trough = int(np.argmin(dd))
    peak_idx = int(np.argmax(equity[: trough + 1]))
    return float(dd[trough]), trough - peak_idx, trough


def beta_alpha(returns: np.ndarray, bench: np.ndarray) -> tuple[float, float]:
    # Filter to common non-null indices.
    mask = ~(np.isnan(returns) | np.isnan(bench))
    r, b = returns[mask], bench[mask]
    if len(r) < 2:
        return 0.0, 0.0
    cov = np.cov(r, b, ddof=1)
    var_b = cov[1, 1]
    if var_b == 0.0:
        return 0.0, 0.0
    beta = cov[0, 1] / var_b
    alpha = float(np.mean(r) - beta * np.mean(b))
    return float(beta), alpha


def information_ratio(returns: np.ndarray, bench: np.ndarray) -> tuple[float, float]:
    diff = returns - bench
    te = float(np.std(diff, ddof=1))
    if te == 0.0:
        return 0.0, 0.0
    return float(np.mean(diff) / te), te


def upside_downside_capture(returns: np.ndarray, bench: np.ndarray) -> tuple[float, float]:
    up = bench > 0
    down = bench < 0
    def _capture(mask):
        if not mask.any():
            return 0.0
        b = float(np.mean(bench[mask]))
        if b == 0.0:
            return 0.0
        return float(np.mean(returns[mask]) / b)
    return _capture(up), _capture(down)


def var_cvar(returns: np.ndarray, percentile: float = 0.05) -> tuple[float, float]:
    if len(returns) == 0:
        return 0.0, 0.0
    var = float(np.quantile(returns, percentile))
    tail = returns[returns <= var]
    cvar = float(np.mean(tail)) if len(tail) > 0 else var
    return var, cvar


def sharpe_se(n_sessions: int, periods: int = 252) -> float:
    """Standard error of the Sharpe estimate for daily returns.

    SE ≈ sqrt(periods / n) for daily-sampling Sharpe.
    Decisions §4 surfaces this in the panel tooltip when n < XENON_PERF_LOW_CONFIDENCE_DAYS.
    """
    if n_sessions <= 0:
        raise ValueError(f"n_sessions must be positive, got {n_sessions!r}")
    return float(np.sqrt(periods / n_sessions))
```

(Add `tail_ratio`, `ulcer`, `skew`, `kurtosis`, `hit_rate` if your `portfolio_performance.py` exposes them and the panel reads them — lift each as a pure function.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest scripts/tests/test_performance_metrics.py -xvs`

Expected: 5 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/reports/performance_metrics.py scripts/tests/test_performance_metrics.py
git commit -m "feat(metrics): extract pure-math performance metrics + sharpe_se"
```

### Task 3.2: `nav_history.py` query module

**Spec §:** Architecture > Components > `src/xenon/db/queries/nav_history.py`.

**Files:**

- Create: `src/xenon/db/queries/nav_history.py`
- Test: `scripts/tests/test_nav_history_queries.py`, `scripts/tests/test_benchmark_cache.py`

- [ ] **Step 1: Write the failing tests**

`scripts/tests/test_nav_history_queries.py`:

```python
import sqlalchemy as sa
from datetime import date
from xenon.db.schema import nav_history
from xenon.db.queries.nav_history import load_nav_curve
from xenon.execution.account_scope import AccountScope


async def test_load_nav_curve_scopes_and_orders(async_engine):
    async with async_engine.begin() as conn:
        for d, nav, env in [
            (date(2026, 5, 30), 100, "live"),
            (date(2026, 5, 29), 99, "live"),
            (date(2026, 5, 30), 200, "sim"),  # different env, must be excluded
        ]:
            await conn.execute(sa.insert(nav_history).values(
                broker="IB", account_env=env, broker_account="DU",
                date=d, nav=str(nav), daily_pnl="0.00", source="intraday",
            ))
    scope = AccountScope(broker="IB", account_env="live", broker_account="DU")
    df = await load_nav_curve(async_engine, scope, period_start=date(2026, 1, 1))
    assert list(df["date"]) == [date(2026, 5, 29), date(2026, 5, 30)]
    assert list(df["nav"]) == [99.0, 100.0]
    assert "sim" not in df.get("account_env", [])  # sanity: scope filter held
```

`scripts/tests/test_benchmark_cache.py`:

```python
import sqlalchemy as sa
from datetime import date
from unittest.mock import MagicMock, AsyncMock
from xenon.db.schema import benchmark_closes
from xenon.db.queries.nav_history import load_benchmark_cached


async def test_cache_hit_returns_df_no_fetch(async_engine):
    async with async_engine.begin() as conn:
        await conn.execute(sa.insert(benchmark_closes).values(
            symbol="SPY", date=date(2026, 5, 30), close="450.00",
        ))
    pool = MagicMock()
    pool.with_role = MagicMock(side_effect=AssertionError("must not fetch"))
    df, err = await load_benchmark_cached(async_engine, pool, "SPY", date(2026, 5, 1))
    assert err is None
    assert len(df) == 1


async def test_cache_miss_then_fetch_then_upsert(async_engine, monkeypatch):
    import pandas as pd
    fetch_called = []
    async def fake_fetch(engine, pool, symbol, period_start):
        fetch_called.append(symbol)
        async with engine.begin() as conn:
            await conn.execute(sa.insert(benchmark_closes).values(
                symbol=symbol, date=date(2026, 5, 30), close="451.00",
            ))
    monkeypatch.setattr(
        "xenon.db.queries.nav_history.fetch_and_cache_benchmark", fake_fetch,
    )
    pool = MagicMock()
    df, err = await load_benchmark_cached(async_engine, pool, "SPY", date(2026, 5, 1))
    assert err is None
    assert fetch_called == ["SPY"]
    assert len(df) == 1
    assert float(df["close"].iloc[0]) == 451.00


async def test_fetch_failure_returns_partial_df_and_error_reason(async_engine, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("ib pool down")
    monkeypatch.setattr(
        "xenon.db.queries.nav_history.fetch_and_cache_benchmark", boom
    )
    pool = MagicMock()
    df, err = await load_benchmark_cached(async_engine, pool, "SPY", date(2026, 5, 1))
    assert err is not None and "ib pool down" in err
    assert len(df) == 0
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest scripts/tests/test_nav_history_queries.py scripts/tests/test_benchmark_cache.py -xvs`

Expected: ImportErrors.

- [ ] **Step 3: Implement `src/xenon/db/queries/nav_history.py`**

```python
"""Query helpers for nav_history and benchmark_closes.

Spec: docs/superpowers/specs/2026-05-31-performance-rebuild-design.md
       § Architecture > Components.
"""
from __future__ import annotations
import logging
from datetime import date
from typing import Tuple
import pandas as pd
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine
from xenon.db.schema import nav_history, benchmark_closes
from xenon.execution.account_scope import AccountScope

logger = logging.getLogger(__name__)


async def load_nav_curve(
    engine: AsyncEngine, scope: AccountScope, period_start: date,
) -> pd.DataFrame:
    """Return a DataFrame[date, nav, daily_pnl, source] ascending by date for the scope."""
    async with engine.begin() as conn:
        rows = (await conn.execute(
            sa.select(
                nav_history.c.date, nav_history.c.nav,
                nav_history.c.daily_pnl, nav_history.c.source,
            ).where(
                (nav_history.c.broker == scope.broker)
                & (nav_history.c.account_env == scope.account_env)
                & (nav_history.c.broker_account == scope.broker_account)
                & (nav_history.c.date >= period_start)
            ).order_by(nav_history.c.date.asc())
        )).all()
    return pd.DataFrame(rows, columns=["date", "nav", "daily_pnl", "source"])


async def load_benchmark_cached(
    engine: AsyncEngine, ib_pool, symbol: str, period_start: date,
) -> Tuple[pd.DataFrame, str | None]:
    """Return (DataFrame[date, close], error_reason_or_None).

    Cache miss → calls fetch_and_cache_benchmark via ib_pool's data role. Any
    fetch failure is caught and surfaced as a non-None error_reason; whatever
    rows ARE cached are still returned so the service can render a partial chart.
    """
    async with engine.begin() as conn:
        rows = (await conn.execute(
            sa.select(benchmark_closes.c.date, benchmark_closes.c.close).where(
                (benchmark_closes.c.symbol == symbol)
                & (benchmark_closes.c.date >= period_start)
            ).order_by(benchmark_closes.c.date.asc())
        )).all()
    df = pd.DataFrame(rows, columns=["date", "close"])

    # Compute missing dates we'd want — for v1 the trigger is "is df empty?".
    # v2 follow-up may diff against the trading calendar to identify mid-window holes.
    if len(df) == 0:
        try:
            await fetch_and_cache_benchmark(engine, ib_pool, symbol, period_start)
            async with engine.begin() as conn:
                rows = (await conn.execute(
                    sa.select(benchmark_closes.c.date, benchmark_closes.c.close).where(
                        (benchmark_closes.c.symbol == symbol)
                        & (benchmark_closes.c.date >= period_start)
                    ).order_by(benchmark_closes.c.date.asc())
                )).all()
            df = pd.DataFrame(rows, columns=["date", "close"])
        except Exception as exc:
            logger.warning("benchmark fetch failed: %s", exc)
            return df, str(exc)
    return df, None


async def fetch_and_cache_benchmark(
    engine: AsyncEngine, ib_pool, symbol: str, period_start: date,
) -> None:
    """Fetch daily closes from IB pool's 'data' role and upsert into benchmark_closes."""
    import asyncio
    def _sync_fetch() -> pd.DataFrame:
        with ib_pool.with_role("data") as ib:
            bars = ib.reqHistoricalData(
                contract=ib_pool.contract_for(symbol),
                endDateTime="",
                durationStr="2 Y",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
        return pd.DataFrame([{"date": b.date, "close": b.close} for b in bars])

    df = await asyncio.get_running_loop().run_in_executor(None, _sync_fetch)
    df = df[df["date"] >= period_start]
    if len(df) == 0:
        return
    async with engine.begin() as conn:
        for _, row in df.iterrows():
            stmt = pg_insert(benchmark_closes).values(
                symbol=symbol, date=row["date"], close=row["close"],
            ).on_conflict_do_update(
                index_elements=["symbol", "date"],
                set_={"close": row["close"]},
            )
            await conn.execute(stmt)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest scripts/tests/test_nav_history_queries.py scripts/tests/test_benchmark_cache.py -xvs`

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/db/queries/nav_history.py \
        scripts/tests/test_nav_history_queries.py \
        scripts/tests/test_benchmark_cache.py
git commit -m "feat(queries): nav_history + benchmark cache helpers"
```

### Task 3.3: `performance.py` service with Phase-0 branching + low-confidence

**Spec §:** Architecture > Components > `src/xenon/api/services/performance.py`; Decisions §4, §8; Phase 0.

**Files:**

- Create: `src/xenon/api/services/performance.py`
- Test: `scripts/tests/test_performance_service.py`, `scripts/tests/test_performance_low_confidence.py`, `scripts/tests/test_ib_dailypnl_assumption.py`

- [ ] **Step 1: Write all the failing tests up front**

`scripts/tests/test_performance_low_confidence.py`:

```python
import numpy as np
import sqlalchemy as sa
from datetime import date, timedelta
import pytest
from xenon.api.services.performance import compute
from xenon.execution.account_scope import AccountScope
from xenon.db.schema import nav_history


async def _seed(async_engine, n, broker="IB", env="sim", start_nav=50000):
    rng = np.random.default_rng(0)
    rets = rng.normal(0.0005, 0.01, n)
    nav = start_nav
    async with async_engine.begin() as conn:
        for i, r in enumerate(rets):
            d = date(2026, 1, 1) + timedelta(days=i)
            new_nav = nav * (1 + r)
            await conn.execute(sa.insert(nav_history).values(
                broker=broker, account_env=env, broker_account="X",
                date=d, nav=str(new_nav), daily_pnl=str(new_nav - nav), source="intraday",
            ))
            nav = new_nav


async def test_n30_IB_low_confidence_true(async_engine, monkeypatch):
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "false")
    await _seed(async_engine, 30)
    data = await compute(async_engine, AccountScope("IB", "sim", "X"))
    assert data["status"] == "ok"
    s = data["summary"]
    assert s["low_confidence"] is True
    assert s["sharpe_ratio"] is not None
    assert s["sharpe_se"] == pytest.approx(np.sqrt(252 / 30), rel=1e-6)
    assert s["sortino_se"] == pytest.approx(np.sqrt(252 / 30), rel=1e-6)


async def test_n126_IB_low_confidence_false(async_engine, monkeypatch):
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "false")
    await _seed(async_engine, 126)
    data = await compute(async_engine, AccountScope("IB", "sim", "X"))
    assert data["summary"]["low_confidence"] is False
    assert data["summary"]["sharpe_se"] is None


async def test_env_override_threshold(async_engine, monkeypatch):
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "false")
    monkeypatch.setenv("XENON_PERF_LOW_CONFIDENCE_DAYS", "60")
    await _seed(async_engine, 60)
    data = await compute(async_engine, AccountScope("IB", "sim", "X"))
    assert data["summary"]["low_confidence"] is False


async def test_FUTU_low_confidence_moot(async_engine):
    await _seed(async_engine, 50, broker="FUTU", env="live")
    data = await compute(async_engine, AccountScope("FUTU", "live", "X"))
    assert data["status"] == "ok"
    assert data["summary"]["low_confidence"] is False  # moot — metrics masked
    assert data["summary"]["sharpe_ratio"] is None
```

`scripts/tests/test_ib_dailypnl_assumption.py`:

```python
import sqlalchemy as sa
from datetime import date, timedelta
import pytest
from xenon.api.services.performance import compute
from xenon.execution.account_scope import AccountScope
from xenon.db.schema import nav_history


async def _seed(async_engine, n, broker="IB", env="sim"):
    async with async_engine.begin() as conn:
        for i in range(n):
            d = date(2026, 1, 1) + timedelta(days=i)
            await conn.execute(sa.insert(nav_history).values(
                broker=broker, account_env=env, broker_account="X",
                date=d, nav=str(50000 + i * 100), daily_pnl="100.00", source="intraday",
            ))


async def test_includes_cashflows_true_masks_IB_metrics(async_engine, monkeypatch):
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "true")
    await _seed(async_engine, 35)
    data = await compute(async_engine, AccountScope("IB", "sim", "X"))
    assert data["status"] == "ok"
    s = data["summary"]
    assert s["sharpe_ratio"] is None
    assert s["sortino_ratio"] is None
    assert "IB TWR requires cash-flow tracking" in " ".join(data["warnings"])


async def test_includes_cashflows_false_populates_IB_metrics(async_engine, monkeypatch):
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "false")
    await _seed(async_engine, 35)
    data = await compute(async_engine, AccountScope("IB", "sim", "X"))
    s = data["summary"]
    assert s["sharpe_ratio"] is not None
    assert "IB TWR requires cash-flow tracking" not in " ".join(data["warnings"])


async def test_FUTU_unaffected_by_env(async_engine, monkeypatch):
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "false")
    await _seed(async_engine, 35, broker="FUTU", env="live")
    data = await compute(async_engine, AccountScope("FUTU", "live", "X"))
    assert data["summary"]["sharpe_ratio"] is None  # masked regardless of env
```

`scripts/tests/test_performance_service.py`:

```python
import sqlalchemy as sa
from datetime import date, timedelta
from unittest.mock import MagicMock
import pytest
from xenon.api.services.performance import compute
from xenon.execution.account_scope import AccountScope
from xenon.db.schema import nav_history, benchmark_closes


async def _seed_nav(async_engine, n, *, broker="IB", env="sim", account="X",
                    start_nav=50000.0, daily_pnl=100.0):
    async with async_engine.begin() as conn:
        nav = start_nav
        for i in range(n):
            d = date(2026, 1, 1) + timedelta(days=i)
            nav += daily_pnl
            await conn.execute(sa.insert(nav_history).values(
                broker=broker, account_env=env, broker_account=account,
                date=d, nav=str(nav), daily_pnl=str(daily_pnl), source="intraday",
            ))


async def test_under_5_rows_returns_insufficient_collecting(async_engine):
    await _seed_nav(async_engine, 3)
    data = await compute(async_engine, AccountScope("IB", "sim", "X"))
    assert data["status"] == "insufficient_history"
    assert data["reason"] == "collecting"
    assert data["days_collected"] == 3
    assert data["hero_net_liq"] is not None


async def test_5_to_30_rows_curve_only(async_engine):
    await _seed_nav(async_engine, 10)
    data = await compute(async_engine, AccountScope("IB", "sim", "X"))
    assert data["status"] == "ok"
    assert data["summary"]["sharpe_ratio"] is None  # masked: under 30 sessions


async def test_30_plus_full_panel_IB(async_engine, monkeypatch):
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "false")
    await _seed_nav(async_engine, 40)
    data = await compute(async_engine, AccountScope("IB", "sim", "X"))
    assert data["status"] == "ok"
    assert data["summary"]["sharpe_ratio"] is not None
    assert data["summary"]["max_drawdown"] is not None


async def test_IB_returns_use_daily_pnl_over_prev_nav(async_engine, monkeypatch):
    """IB return formula must be daily_pnl/prev_nav — NOT (nav_t-nav_{t-1})/nav_{t-1}.
    Construct a seed where the two formulas would diverge."""
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "false")
    async with async_engine.begin() as conn:
        # day 1: nav=100, daily_pnl=0
        await conn.execute(sa.insert(nav_history).values(
            broker="IB", account_env="sim", broker_account="X",
            date=date(2026, 1, 1), nav="100.0", daily_pnl="0.0", source="intraday",
        ))
        # day 2: nav=200 (e.g. $100 deposit), daily_pnl=5 (only $5 of trading)
        # nav-delta formula would say return = 100/100 = +100%
        # daily_pnl/prev_nav formula = 5/100 = +5% (correct trading return)
        await conn.execute(sa.insert(nav_history).values(
            broker="IB", account_env="sim", broker_account="X",
            date=date(2026, 1, 2), nav="200.0", daily_pnl="5.0", source="intraday",
        ))
        # pad to >= 5 so we hit status=ok
        for i in range(3, 8):
            await conn.execute(sa.insert(nav_history).values(
                broker="IB", account_env="sim", broker_account="X",
                date=date(2026, 1, i), nav="200.0", daily_pnl="0.0", source="intraday",
            ))
    data = await compute(async_engine, AccountScope("IB", "sim", "X"))
    # series[1] daily_return should be 5/100 = 0.05, NOT 1.0
    series = data["series"]
    assert series[1]["daily_return"] == pytest.approx(0.05, rel=1e-9)


async def test_FUTU_returns_use_nav_delta(async_engine):
    async with async_engine.begin() as conn:
        await conn.execute(sa.insert(nav_history).values(
            broker="FUTU", account_env="live", broker_account="X",
            date=date(2026, 1, 1), nav="100.0", daily_pnl="0.0", source="intraday",
        ))
        await conn.execute(sa.insert(nav_history).values(
            broker="FUTU", account_env="live", broker_account="X",
            date=date(2026, 1, 2), nav="110.0", daily_pnl="999.0",  # daily_pnl IGNORED
            source="intraday",
        ))
        for i in range(3, 8):
            await conn.execute(sa.insert(nav_history).values(
                broker="FUTU", account_env="live", broker_account="X",
                date=date(2026, 1, i), nav="110.0", daily_pnl="0.0", source="intraday",
            ))
    data = await compute(async_engine, AccountScope("FUTU", "live", "X"))
    # FUTU uses (nav-prev)/prev — 10/100 = 0.10, NOT 999/100 = 9.99
    series = data["series"]
    assert series[1]["equity"] == 110.0
    # FUTU does not populate Sharpe (masked) — sanity
    assert data["summary"]["sharpe_ratio"] is None


async def test_scope_isolation_ib_vs_futu(async_engine, monkeypatch):
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "false")
    await _seed_nav(async_engine, 10, broker="IB", env="sim", account="X", start_nav=50000)
    await _seed_nav(async_engine, 10, broker="FUTU", env="live", account="X", start_nav=200000)
    ib = await compute(async_engine, AccountScope("IB", "sim", "X"))
    fu = await compute(async_engine, AccountScope("FUTU", "live", "X"))
    assert ib["summary"]["ending_equity"] != fu["summary"]["ending_equity"]
    assert ib["scope"]["broker"] == "IB"
    assert fu["scope"]["broker"] == "FUTU"


async def test_benchmark_missing_marks_bench_fields_null(async_engine, monkeypatch):
    monkeypatch.setenv("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", "false")
    await _seed_nav(async_engine, 40)
    # No benchmark_closes rows, and provide a pool whose fetch will raise
    pool = MagicMock()
    async def boom(*a, **kw):
        raise RuntimeError("ib pool down")
    monkeypatch.setattr("xenon.db.queries.nav_history.fetch_and_cache_benchmark", boom)
    data = await compute(async_engine, AccountScope("IB", "sim", "X"), ib_pool=pool)
    assert data["status"] == "ok"
    assert data["benchmark"] is None
    assert data["summary"]["beta"] is None
    assert any("benchmark_unavailable" in w for w in data["warnings"])
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest scripts/tests/test_performance_service.py scripts/tests/test_performance_low_confidence.py scripts/tests/test_ib_dailypnl_assumption.py -xvs`

Expected: ImportErrors.

- [ ] **Step 3: Implement the service**

`src/xenon/api/services/performance.py`:

```python
"""Inline FastAPI service computing /performance from xenon.nav_history.

Spec: docs/superpowers/specs/2026-05-31-performance-rebuild-design.md
"""
from __future__ import annotations
import os
import logging
import numpy as np
from typing import Any
from sqlalchemy.ext.asyncio import AsyncEngine
from xenon.execution.account_scope import AccountScope
from xenon.db.queries.nav_history import load_nav_curve, load_benchmark_cached
from xenon.reports import performance_metrics as M
from xenon.utils.market_calendar import current_session_date_et

logger = logging.getLogger(__name__)

PERIODS_PER_YEAR = 252

# Field groups for masking. Keep in sync with web/lib/types.ts PerformanceSummary.
ANNUALIZED_RISK_FIELDS = (
    "sharpe_ratio", "sortino_ratio", "calmar_ratio",
    "annualized_return", "annualized_volatility", "downside_deviation",
    "var_95", "cvar_95", "tail_ratio", "ulcer_index",
)
BENCH_RELATIVE_FIELDS = (
    "beta", "alpha", "correlation", "r_squared",
    "tracking_error", "information_ratio", "treynor_ratio",
    "upside_capture", "downside_capture",
)
DISTRIBUTION_FIELDS = (
    "hit_rate", "positive_days", "negative_days", "flat_days",
    "best_day", "worst_day", "average_up_day", "average_down_day",
    "win_loss_ratio", "skew", "kurtosis",
)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key, "").strip().lower()
    if v in ("true", "1", "yes"):
        return True
    if v in ("false", "0", "no"):
        return False
    return default


def _ib_should_mask_metrics() -> bool:
    """Phase 0 gate. Default True (safe-but-pessimistic)."""
    return _env_bool("XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS", True)


async def compute(
    engine: AsyncEngine, scope: AccountScope, *, ib_pool=None, as_of=None,
) -> dict[str, Any]:
    period_start = _period_start(as_of)
    curve = await load_nav_curve(engine, scope, period_start)
    days_collected = len(curve)
    min_curve = _env_int("XENON_PERF_MIN_DAYS_CURVE", 5)
    min_metrics = _env_int("XENON_PERF_MIN_DAYS_METRICS", 30)
    low_conf_threshold = _env_int("XENON_PERF_LOW_CONFIDENCE_DAYS", 126)

    if days_collected < min_curve:
        return _insufficient(
            reason="collecting",
            days_collected=days_collected,
            hero_net_liq=float(curve["nav"].iloc[-1]) if days_collected else None,
            inception=str(curve["date"].iloc[0]) if days_collected else None,
        )

    bench_df, bench_err = (await load_benchmark_cached(engine, ib_pool, "SPY", period_start)) \
        if ib_pool is not None else (None, "ib_pool unavailable")

    # Pick the return series per broker
    if scope.broker == "IB":
        nav = curve["nav"].astype(float).to_numpy()
        dp  = curve["daily_pnl"].astype(float).fillna(0.0).to_numpy()
        prev = np.concatenate(([nav[0]], nav[:-1]))
        returns = np.where(prev > 0, dp / prev, 0.0)
    else:  # FUTU
        nav = curve["nav"].astype(float).to_numpy()
        prev = np.concatenate(([nav[0]], nav[:-1]))
        returns = np.where(prev > 0, (nav - prev) / prev, 0.0)
        returns[0] = 0.0

    warnings: list[str] = []
    summary = _base_summary(nav, returns, days_collected)

    metrics_unlocked = days_collected >= min_metrics
    futu_mask = scope.broker == "FUTU"
    ib_mask = scope.broker == "IB" and _ib_should_mask_metrics()
    if ib_mask:
        warnings.append(
            "IB TWR requires cash-flow tracking — follow-up. "
            "See docs/superpowers/reports/2026-06-01-ib-dailypnl-verification.md."
        )
    if futu_mask:
        warnings.append(
            "FUTU NAV-change returns include external cash flows "
            "(deposits, withdrawals, dividends). True Time-Weighted Return requires "
            "cash-flow tracking — follow-up."
        )

    risk_masked = futu_mask or ib_mask or not metrics_unlocked
    if not risk_masked:
        _fill_annualized(summary, returns, bench_df)
        _fill_distribution(summary, returns)
    if not risk_masked and bench_err:
        warnings.append(f"benchmark_unavailable: {bench_err}")
        for k in BENCH_RELATIVE_FIELDS:
            summary[k] = None

    summary["low_confidence"] = (
        metrics_unlocked and not risk_masked and days_collected < low_conf_threshold
    )
    if summary["low_confidence"]:
        summary["sharpe_se"] = M.sharpe_se(days_collected, periods=PERIODS_PER_YEAR)
        summary["sortino_se"] = M.sharpe_se(days_collected, periods=PERIODS_PER_YEAR)  # same formula
    else:
        summary["sharpe_se"] = None
        summary["sortino_se"] = None

    series = _build_series(curve, bench_df)

    return {
        "status": "ok",
        "as_of": str(current_session_date_et()),
        "last_sync": str(curve["date"].iloc[-1]),
        "period_start": str(period_start),
        "period_end": str(curve["date"].iloc[-1]),
        "period_label": _period_label(curve["date"].iloc[0]),
        "scope": {
            "broker": scope.broker, "account_env": scope.account_env,
            "broker_account": scope.broker_account,
        },
        "currency": "USD",
        "benchmark": "SPY" if bench_df is not None and len(bench_df) else None,
        "benchmark_total_return": _bench_total_return(bench_df),
        "trades_source": "nav_history",
        "methodology": {"basis": "NAV change", "annualization_periods": PERIODS_PER_YEAR},
        "price_sources": {"primary": "nav_history", "benchmark": "ib_historical_daily"},
        "summary": summary,
        "series": series,
        "warnings": warnings,
        "contracts_missing_history": [],
    }


def _base_summary(nav, returns, n) -> dict[str, Any]:
    start, end = float(nav[0]), float(nav[-1])
    depth, dur, _ = M.max_drawdown(nav)
    return {
        "starting_equity": start, "ending_equity": end,
        "pnl": end - start, "total_return": (end - start) / start if start else 0.0,
        "trading_days": n,
        "max_drawdown": depth, "max_drawdown_duration_days": dur,
        "current_drawdown": (end - float(np.maximum.accumulate(nav)[-1])) / float(np.maximum.accumulate(nav)[-1]),
        # placeholders for nullable fields, filled by _fill_* below
        **{k: None for k in ANNUALIZED_RISK_FIELDS},
        **{k: None for k in BENCH_RELATIVE_FIELDS},
        **{k: None for k in DISTRIBUTION_FIELDS},
    }


def _fill_annualized(summary, returns, bench_df) -> None:
    summary["sharpe_ratio"] = M.sharpe(returns)
    summary["sortino_ratio"] = M.sortino(returns)
    summary["annualized_return"] = float(np.mean(returns) * PERIODS_PER_YEAR)
    summary["annualized_volatility"] = float(np.std(returns, ddof=1) * np.sqrt(PERIODS_PER_YEAR))
    summary["downside_deviation"] = float(
        np.std(returns[returns < 0], ddof=1) * np.sqrt(PERIODS_PER_YEAR)
    ) if (returns < 0).any() else 0.0
    summary["var_95"], summary["cvar_95"] = M.var_cvar(returns, percentile=0.05)
    if bench_df is not None and len(bench_df):
        bench_returns = bench_df["close"].astype(float).pct_change().fillna(0.0).to_numpy()
        m = min(len(returns), len(bench_returns))
        beta, alpha = M.beta_alpha(returns[-m:], bench_returns[-m:])
        ir, te = M.information_ratio(returns[-m:], bench_returns[-m:])
        up, down = M.upside_downside_capture(returns[-m:], bench_returns[-m:])
        summary["beta"] = beta
        summary["alpha"] = alpha
        summary["information_ratio"] = ir
        summary["tracking_error"] = te
        summary["upside_capture"] = up
        summary["downside_capture"] = down


def _fill_distribution(summary, returns) -> None:
    pos = int((returns > 0).sum())
    neg = int((returns < 0).sum())
    flat = int((returns == 0).sum())
    summary["positive_days"] = pos
    summary["negative_days"] = neg
    summary["flat_days"] = flat
    summary["hit_rate"] = pos / (pos + neg) if (pos + neg) else 0.0
    summary["best_day"] = float(returns.max())
    summary["worst_day"] = float(returns.min())


def _insufficient(*, reason, days_collected, hero_net_liq, inception) -> dict[str, Any]:
    return {
        "status": "insufficient_history", "reason": reason,
        "days_collected": days_collected,
        "days_required_for_curve": _env_int("XENON_PERF_MIN_DAYS_CURVE", 5),
        "days_required_for_metrics": _env_int("XENON_PERF_MIN_DAYS_METRICS", 30),
        "inception_date": inception, "hero_net_liq": hero_net_liq, "currency": "USD",
    }


def _period_start(as_of):
    import datetime as dt
    today = as_of or current_session_date_et()
    return dt.date(today.year, 1, 1)


def _period_label(inception):
    import datetime as dt
    return (
        "YTD NAV Change"
        if inception <= dt.date(inception.year, 1, 2)
        else "INCEPTION-TO-DATE NAV CHANGE"
    )


def _bench_total_return(bench_df):
    if bench_df is None or len(bench_df) < 2:
        return None
    first = float(bench_df["close"].iloc[0])
    last = float(bench_df["close"].iloc[-1])
    return (last - first) / first if first else None


def _build_series(curve, bench_df) -> list[dict[str, Any]]:
    peak = curve["nav"].cummax()
    drawdown = (curve["nav"] - peak) / peak
    bench_map = {} if bench_df is None else dict(zip(bench_df["date"], bench_df["close"].astype(float)))
    series = []
    prev_close = None
    for _, row in curve.iterrows():
        close = bench_map.get(row["date"])
        bret = None if (close is None or prev_close is None or prev_close == 0) \
            else (close - prev_close) / prev_close
        series.append({
            "date": str(row["date"]),
            "equity": float(row["nav"]),
            "daily_return": (float(row["daily_pnl"]) / float(row["nav"]))
                            if row["daily_pnl"] is not None and float(row["nav"]) > 0 else None,
            "drawdown": float(drawdown.loc[row.name]),
            "benchmark_close": close,
            "benchmark_return": bret,
        })
        if close is not None:
            prev_close = close
    return series
```

- [ ] **Step 4: Run all three new test files to verify pass**

Run: `uv run pytest scripts/tests/test_performance_service.py scripts/tests/test_performance_low_confidence.py scripts/tests/test_ib_dailypnl_assumption.py -xvs`

Expected: all PASS. Iterate on the service code until they do — do not move on with red tests.

- [ ] **Step 5: Commit**

```bash
git add src/xenon/api/services/performance.py \
        scripts/tests/test_performance_service.py \
        scripts/tests/test_performance_low_confidence.py \
        scripts/tests/test_ib_dailypnl_assumption.py
git commit -m "feat(perf): inline performance service with Phase-0 mask + low-confidence"
```

### Task 3.4: Scope-keyed memoize with market-aware TTL

**Spec §:** Decisions §6 — "thin scope-keyed memoize in the FastAPI service ... market-aware TTL (60s open / 30min closed)".

**Files:**

- Create: `src/xenon/api/services/perf_cache.py`
- Modify: `src/xenon/api/services/performance.py` (wire the memoize in)
- Test: `scripts/tests/test_perf_cache.py`

- [ ] **Step 1: Write the failing test**

`scripts/tests/test_perf_cache.py`:

```python
import time
import pytest
from unittest.mock import AsyncMock
from xenon.api.services.perf_cache import cached_compute, _ttl_for_now
from xenon.execution.account_scope import AccountScope


async def test_cache_hit_within_ttl_returns_same_object(monkeypatch):
    inner = AsyncMock(return_value={"status": "ok", "computed_at": "v1"})
    monkeypatch.setattr("xenon.api.services.perf_cache._inner_compute", inner)
    scope = AccountScope("IB", "sim", "X")
    r1 = await cached_compute(None, scope, ib_pool=None)
    r2 = await cached_compute(None, scope, ib_pool=None)
    assert r1 is r2
    assert inner.call_count == 1


async def test_cache_miss_for_different_scope(monkeypatch):
    inner = AsyncMock(side_effect=[{"x": 1}, {"x": 2}])
    monkeypatch.setattr("xenon.api.services.perf_cache._inner_compute", inner)
    await cached_compute(None, AccountScope("IB", "sim", "X"), ib_pool=None)
    await cached_compute(None, AccountScope("FUTU", "live", "X"), ib_pool=None)
    assert inner.call_count == 2


def test_ttl_market_aware():
    """TTL is 60s during open hours, 1800s (30 min) otherwise."""
    import datetime as dt
    open_dt  = dt.datetime(2026, 6, 1, 10, 0)  # Mon 10:00 ET — open
    closed_dt = dt.datetime(2026, 6, 1, 20, 0)  # Mon 20:00 ET — closed
    weekend   = dt.datetime(2026, 6, 6, 12, 0)  # Sat — closed
    assert _ttl_for_now(open_dt)   == 60
    assert _ttl_for_now(closed_dt) == 1800
    assert _ttl_for_now(weekend)   == 1800
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_perf_cache.py -xvs`

Expected: ImportError.

- [ ] **Step 3: Implement `perf_cache.py`**

```python
"""Tiny scope-keyed memoize for compute(). Market-aware TTL.

Spec: Decisions §6 — 60s during US market hours, 30min otherwise.
"""
from __future__ import annotations
import time
import datetime as dt
from typing import Any
import zoneinfo
from xenon.execution.account_scope import AccountScope
from xenon.api.services.performance import compute as _inner_compute

_ET = zoneinfo.ZoneInfo("America/New_York")
_TTL_OPEN_SEC = 60
_TTL_CLOSED_SEC = 30 * 60

# {(broker, account_env, broker_account): (result, stored_at_epoch)}
_cache: dict[tuple[str, str, str], tuple[Any, float]] = {}


def _ttl_for_now(now: dt.datetime | None = None) -> int:
    """Returns TTL in seconds. 60 during 9:30–16:00 ET Mon–Fri, 1800 otherwise."""
    now = now or dt.datetime.now(tz=_ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_ET)
    else:
        now = now.astimezone(_ET)
    if now.weekday() >= 5:  # Sat/Sun
        return _TTL_CLOSED_SEC
    minutes = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= minutes < 16 * 60:
        return _TTL_OPEN_SEC
    return _TTL_CLOSED_SEC


def _key(scope: AccountScope) -> tuple[str, str, str]:
    return (scope.broker, scope.account_env, scope.broker_account)


async def cached_compute(engine, scope: AccountScope, *, ib_pool=None) -> Any:
    k = _key(scope)
    ttl = _ttl_for_now()
    now = time.time()
    cached = _cache.get(k)
    if cached is not None and (now - cached[1]) < ttl:
        return cached[0]
    result = await _inner_compute(engine, scope, ib_pool=ib_pool)
    _cache[k] = (result, now)
    return result


def warm(engine, scope: AccountScope, *, ib_pool=None) -> None:
    """Fire-and-forget warmup. Used by deprecated POST /performance/background."""
    import asyncio
    asyncio.create_task(cached_compute(engine, scope, ib_pool=ib_pool))
```

- [ ] **Step 4: Wire the route to use `cached_compute` instead of raw `compute`**

In `src/xenon/api/routes/performance.py`, change the import from:

```python
from xenon.api.services.performance import compute, _insufficient
```

to:

```python
from xenon.api.services.performance import _insufficient
from xenon.api.services.perf_cache import cached_compute as compute
```

(All call sites stay the same — `compute(engine, scope, ib_pool=...)`.)

- [ ] **Step 5: Run all the perf_cache + route tests to verify**

Run: `uv run pytest scripts/tests/test_perf_cache.py scripts/tests/test_performance_route.py -xvs`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/xenon/api/services/perf_cache.py \
        src/xenon/api/routes/performance.py \
        scripts/tests/test_perf_cache.py
git commit -m "feat(perf): scope-keyed memoize with market-aware TTL"
```

---

## Phase 4 — FastAPI wiring

### Task 4.1: `get_performance_scope` dep + GET route

**Spec §:** Architecture > Components > `src/xenon/api/routes/performance.py`.

**Files:**

- Create: `src/xenon/api/routes/performance.py`
- Modify: `src/xenon/api/guards.py` (add `get_performance_scope`)
- Test: `scripts/tests/test_performance_route.py`

- [ ] **Step 1: Write failing tests**

`scripts/tests/test_performance_route.py`:

```python
from fastapi.testclient import TestClient
from xenon.api.server import app


def test_get_performance_ib_returns_200():
    with TestClient(app) as client:
        r = client.get("/performance?broker=IB")
        assert r.status_code == 200
        body = r.json()
        assert body["scope"]["broker"] == "IB"


def test_get_performance_no_broker_returns_400():
    with TestClient(app) as client:
        r = client.get("/performance")
        assert r.status_code == 400


def test_get_performance_garbage_broker_returns_400():
    with TestClient(app) as client:
        r = client.get("/performance?broker=GARBAGE")
        assert r.status_code == 400


def test_get_performance_futu_cold_start_returns_insufficient():
    # app.state.futu_account is None at fresh boot
    with TestClient(app) as client:
        r = client.get("/performance?broker=FUTU")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "insufficient_history"
        assert body["reason"] == "futu_not_synced"


def test_deprecated_POST_performance_defaults_to_IB():
    with TestClient(app) as client:
        r = client.post("/performance")
        assert r.status_code == 200
        assert r.json()["scope"]["broker"] == "IB"


def test_deprecated_POST_background_returns_202():
    with TestClient(app) as client:
        r = client.post("/performance/background")
        assert r.status_code == 202
        assert r.json() == {"status": "accepted"}
```

- [ ] **Step 2: Run to verify failures**

Run: `uv run pytest scripts/tests/test_performance_route.py -xvs`

Expected: 404s / endpoint missing.

- [ ] **Step 3: Add `get_performance_scope` dep in `guards.py`**

```python
def get_performance_scope(request: Request) -> AccountScope | tuple[str, str]:
    """Resolve scope for /performance.

    Returns AccountScope normally, or a special sentinel ("insufficient", reason)
    if FUTU was requested but no account is known. The route turns the sentinel
    into a 200 insufficient_history response.
    """
    broker = request.query_params.get("broker", "").upper().strip()
    if broker not in ("IB", "FUTU"):
        raise HTTPException(400, detail="broker must be IB or FUTU")
    if broker == "IB":
        return AccountScope(
            broker="IB",
            account_env=request.app.state.trading_mode,
            broker_account=request.app.state.broker_account,
        )
    futu_account = getattr(request.app.state, "futu_account", None)
    futu_trd_env = getattr(request.app.state, "futu_trd_env", None)
    if futu_account is None or futu_trd_env is None:
        return ("insufficient", "futu_not_synced")
    return AccountScope(
        broker="FUTU",
        account_env=env_from_trd_env(futu_trd_env),
        broker_account=futu_account,
    )
```

- [ ] **Step 4: Implement the route**

`src/xenon/api/routes/performance.py`:

```python
from __future__ import annotations
from fastapi import APIRouter, Depends, Request, Response
from xenon.api.guards import get_performance_scope
from xenon.api.services.performance import compute, _insufficient
from xenon.db.engine import get_engine

router = APIRouter()


@router.get("/performance")
async def get_performance(request: Request, scope=Depends(get_performance_scope)):
    if isinstance(scope, tuple) and scope[0] == "insufficient":
        return _insufficient(
            reason=scope[1], days_collected=0,
            hero_net_liq=_futu_cached_hero(),
            inception=None,
        )
    engine = get_engine()
    pool = getattr(request.app.state, "ib_pool", None)
    return await compute(engine, scope, ib_pool=pool)


@router.post("/performance")
async def deprecated_post(request: Request):
    """Deprecated: defaults broker=IB, proxies the GET."""
    request.scope["query_string"] = b"broker=IB"
    return await get_performance(request, scope=get_performance_scope(request))


@router.post("/performance/background", status_code=202)
async def deprecated_post_background(request: Request):
    """Deprecated: kept for 202 fire-and-forget contract.

    Per spec § Error handling row 'POST /performance/background 202 contract':
    returns 202 immediately AND kicks off a background warmup of the IB-scope
    memoize so the next GET is fast.
    """
    from xenon.api.services.perf_cache import warm
    from xenon.execution.account_scope import AccountScope
    ib_scope = AccountScope(
        broker="IB",
        account_env=request.app.state.trading_mode,
        broker_account=request.app.state.broker_account,
    )
    warm(get_engine(), ib_scope, ib_pool=getattr(request.app.state, "ib_pool", None))
    return {"status": "accepted"}


def _futu_cached_hero() -> float | None:
    import json, os
    path = "data/futu_portfolio.json"
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        return float(data["account_summary"]["net_liquidation"])
    except Exception:
        return None
```

- [ ] **Step 5: Register the router in `server.py`**

In `src/xenon/api/server.py`, near the other `app.include_router(...)` calls, add:

```python
from xenon.api.routes.performance import router as performance_router
app.include_router(performance_router)
```

Remove (or comment out, then remove in Task 4.2) the existing in-line POST `/performance` and POST `/performance/background` definitions in server.py — the new router owns those paths.

- [ ] **Step 6: Run tests**

Run: `uv run pytest scripts/tests/test_performance_route.py -xvs`

Expected: 6 PASS.

- [ ] **Step 7: Commit**

```bash
git add src/xenon/api/routes/performance.py src/xenon/api/guards.py \
        src/xenon/api/server.py scripts/tests/test_performance_route.py
git commit -m "feat(api): GET /performance route + get_performance_scope dep"
```

### Task 4.2: `POST /futu/sync` persist + lifespan warming

**Spec §:** Persistence flow (FUTU NAV); Decisions §9.

**Files:**

- Modify: `src/xenon/api/server.py` (futu_sync handler + lifespan)
- Test: `scripts/tests/test_futu_account_warming.py`

- [ ] **Step 1: Write the failing test**

`scripts/tests/test_futu_account_warming.py`:

```python
import sqlalchemy as sa
from datetime import date
from fastapi.testclient import TestClient
from xenon.db.schema import nav_history


def test_lifespan_warms_app_state_from_latest_futu_row(sync_engine):
    with sync_engine.begin() as conn:
        conn.execute(sa.insert(nav_history).values(
            broker="FUTU", account_env="live", broker_account="42",
            date=date(2026, 5, 30), nav="100000.00", daily_pnl="0.00", source="intraday",
        ))
    # Importing app triggers the lifespan when entering TestClient's context.
    from xenon.api.server import app
    with TestClient(app) as client:
        assert app.state.futu_account == "42"
        assert app.state.futu_trd_env == "REAL"


def test_lifespan_no_futu_rows_leaves_state_None(sync_engine):
    with sync_engine.begin() as conn:
        conn.execute(sa.delete(nav_history).where(nav_history.c.broker == "FUTU"))
    from xenon.api.server import app
    with TestClient(app) as client:
        assert app.state.futu_account is None
        assert app.state.futu_trd_env is None


def test_lifespan_skips_warming_on_unknown_account_env(sync_engine, caplog):
    with sync_engine.begin() as conn:
        conn.execute(sa.insert(nav_history).values(
            broker="FUTU", account_env="legacy", broker_account="99",
            date=date(2026, 5, 30), nav="50000.00", daily_pnl="0.00", source="intraday",
        ))
    from xenon.api.server import app
    with TestClient(app) as client:
        assert app.state.futu_account is None
        assert app.state.futu_trd_env is None
    assert any("unknown account_env" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest scripts/tests/test_futu_account_warming.py -xvs`

Expected: `app.state.futu_account` not present.

- [ ] **Step 3: Patch the lifespan**

In `src/xenon/api/server.py`, find the lifespan context manager (search for `@asynccontextmanager` near app creation). Inside the startup block, after engine init, add:

```python
    # Warm FUTU scope identity from the latest FUTU nav_history row so
    # GET /performance?broker=FUTU works after restart without a fresh /futu/sync.
    async with engine.begin() as conn:
        row = (await conn.execute(
            sa.select(nav_history.c.broker_account, nav_history.c.account_env)
              .where(nav_history.c.broker == "FUTU")
              .order_by(nav_history.c.date.desc())
              .limit(1)
        )).first()
    if row:
        env_map = {"live": "REAL", "sim": "SIMULATE"}
        if row.account_env not in env_map:
            logger.warning(
                "lifespan futu warm: unknown account_env=%s for account=%s — skipping",
                row.account_env, row.broker_account,
            )
            app.state.futu_account = None
            app.state.futu_trd_env = None
        else:
            app.state.futu_account = row.broker_account
            app.state.futu_trd_env = env_map[row.account_env]
    else:
        app.state.futu_account = None
        app.state.futu_trd_env = None
```

- [ ] **Step 4: Patch the `/futu/sync` handler**

Find `async def futu_sync(...)` in `src/xenon/api/server.py`. Change the signature:

```python
@app.post("/futu/sync")
async def futu_sync(request: Request):
```

Inside the body, after the existing `await _atomic_save(result)` (or equivalent), add:

```python
    client = _futu_client
    if client is not None and client.is_connected() and client._acc_id is not None:
        matched_env = client.trd_env_of_matched_account()
        if matched_env is None:
            logger.warning("futu_sync: matched trd_env unknown — skipping nav persist")
        else:
            request.app.state.futu_account = str(client._acc_id)
            request.app.state.futu_trd_env = matched_env
            from xenon.api.services.futu_nav_persistence import persist_futu_nav
            await persist_futu_nav(
                engine=get_engine(),
                futu_client=client,
                matched_trd_env=matched_env,
                payload=result,
            )
```

- [ ] **Step 5: Add 409 handler for `NavAccountEnvConflict`**

Near the FastAPI app definition, add:

```python
from xenon.api.services.futu_nav_persistence import NavAccountEnvConflict

@app.exception_handler(NavAccountEnvConflict)
async def _nav_conflict_handler(request, exc):
    return JSONResponse(
        status_code=409,
        content={"error": f"nav account_env conflict for ({exc.scope.broker}, {exc.scope.broker_account})"},
    )
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest scripts/tests/test_futu_account_warming.py -xvs`

Expected: 3 PASS.

- [ ] **Step 7: Commit**

```bash
git add src/xenon/api/server.py scripts/tests/test_futu_account_warming.py
git commit -m "feat(api): lifespan warm futu state; /futu/sync persists nav"
```

### Task 4.3: Wire `xenon-futu-sync` CLI through the shared persist helper

**Spec §:** Architecture > Components > `futu_nav_persistence.py` — "Called from BOTH FastAPI POST /futu/sync AND the xenon-futu-sync CLI via the same helper."

**Files:**

- Modify: `src/xenon/execution/futu_sync.py` (the CLI entry point — verify the actual path with `grep -rn 'xenon-futu-sync' pyproject.toml`)
- Test: `scripts/tests/test_futu_sync_cli_persists.py`

- [ ] **Step 1: Confirm the CLI entry-point path**

Run: `grep -n "xenon-futu-sync\|futu_sync" pyproject.toml`

The `[project.scripts]` section names the entry. Read the target module to learn its existing `main()` shape — most likely `xenon.execution.futu_sync:main` or similar.

- [ ] **Step 2: Write the failing test**

`scripts/tests/test_futu_sync_cli_persists.py`:

```python
import sqlalchemy as sa
from unittest.mock import patch, MagicMock
from xenon.db.schema import nav_history


def test_cli_main_persists_nav_row(sync_engine, monkeypatch):
    fake_client = MagicMock()
    fake_client.is_connected.return_value = True
    fake_client._acc_id = 42
    fake_client.trd_env_of_matched_account.return_value = "REAL"
    fake_client.fetch_positions.return_value = {
        "account_summary": {"net_liquidation": 100000.00},
        "positions": [],
    }
    with patch("xenon.execution.futu_sync.FutuClient", return_value=fake_client):
        from xenon.execution.futu_sync import main
        rc = main([])
    assert rc == 0
    with sync_engine.begin() as conn:
        row = conn.execute(sa.select(nav_history).where(nav_history.c.broker == "FUTU")).first()
    assert row is not None
    assert row.broker_account == "42"
    assert row.account_env == "live"
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest scripts/tests/test_futu_sync_cli_persists.py -xvs`

Expected: assertion fails (no row written) — the CLI does not yet call `persist_futu_nav`.

- [ ] **Step 4: Patch the CLI**

In `src/xenon/execution/futu_sync.py`'s `main()`, after the call that produces `result` (the positions/account_summary dict) and before the existing JSON-write or print step, add:

```python
    if client.is_connected() and client._acc_id is not None:
        matched_env = client.trd_env_of_matched_account()
        if matched_env is None:
            logger.warning("futu-sync CLI: matched trd_env unknown — skipping nav persist")
        else:
            from xenon.api.services.futu_nav_persistence import persist_futu_nav
            from xenon.db.engine import get_sync_engine
            import asyncio
            # CLI is sync; bridge to the async helper. The sync engine is fine
            # for one-shot use; we adapt with run() since persist_futu_nav is async.
            asyncio.run(persist_futu_nav(
                engine=_async_engine_for_cli(),  # see Step 5
                futu_client=client,
                matched_trd_env=matched_env,
                payload=result,
            ))
```

- [ ] **Step 5: Add the async engine helper used above**

If `xenon.db.engine` exposes an `async_engine_for_subprocess()` or equivalent, use that. Otherwise add a tiny helper at module scope inside `futu_sync.py`:

```python
def _async_engine_for_cli():
    from sqlalchemy.ext.asyncio import create_async_engine
    import os
    url = os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+asyncpg://", 1)
    return create_async_engine(url)
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest scripts/tests/test_futu_sync_cli_persists.py -xvs`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/xenon/execution/futu_sync.py scripts/tests/test_futu_sync_cli_persists.py
git commit -m "feat(cli): xenon-futu-sync persists nav via shared helper"
```

---

## Phase 5 — Web contract

### Task 5.1: `types.ts` discriminated union + nullable fields + low-confidence

**Spec §:** Architecture > Type contract; Decisions §4.

**Files:**

- Modify: `web/lib/types.ts`

- [ ] **Step 1: Replace `PerformanceData`-related types**

Open `web/lib/types.ts` and replace the existing `PerformanceData` / `PerformanceSummary` / `PerformanceSeriesPoint` block with the **exact** TypeScript from the spec § "Type contract", including:

- `low_confidence: boolean` on `PerformanceSummary`
- `sharpe_se: number | null`
- `sortino_se: number | null`

(The spec block is canonical; copy it verbatim. Adjust imports if any.)

- [ ] **Step 2: Run typecheck**

Run: `cd web && npm run typecheck`

Expected: many errors in `usePerformance.ts`, `performanceChart.ts`, `PerformancePanel.tsx`. Those are fixed in tasks 5.2–5.4 — that's the point of doing this first.

- [ ] **Step 3: Commit (with broken consumers — they're the next tasks)**

```bash
git add web/lib/types.ts
git commit -m "feat(web): discriminated PerformanceData union + low_confidence fields"
```

### Task 5.2: `usePerformance.ts` broker-aware signature

**Files:**

- Modify: `web/lib/usePerformance.ts`
- Test: `web/tests/usePerformance.test.ts`

- [ ] **Step 1: Write the failing test**

`web/tests/usePerformance.test.ts`:

```typescript
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { usePerformance } from "@/lib/usePerformance";

const fetchMock = vi.fn();
beforeEach(() => {
  global.fetch = fetchMock as any;
  fetchMock.mockReset();
});

describe("usePerformance", () => {
  it("requests broker=IB when activeAccount is ib", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ status: "ok" }),
    });
    const { result } = renderHook(() => usePerformance(true, "ib"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(fetchMock.mock.calls[0][0]).toContain("broker=IB");
  });

  it("re-fetches when activeAccount changes", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ status: "ok" }),
    });
    const { rerender } = renderHook(({ acc }) => usePerformance(true, acc), {
      initialProps: { acc: "ib" as const },
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    rerender({ acc: "futu" as const });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[1][0]).toContain("broker=FUTU");
  });
});
```

- [ ] **Step 2: Run to verify failure**

Run: `cd web && npm test -- usePerformance.test.ts`

Expected: signature error / fetch not called with broker param.

- [ ] **Step 3: Update the hook**

In `web/lib/usePerformance.ts`, change the signature and body:

```typescript
export function usePerformance(active: boolean, activeAccount: "ib" | "futu") {
  const url = `/api/performance?broker=${activeAccount.toUpperCase()}`;
  // SWR cache key includes broker so IB and FUTU don't collide
  return useSWR(active ? [`performance`, activeAccount] : null, () =>
    fetch(url).then((r) => r.json()),
  );
}

export function extractTimestamp(
  data: PerformanceData | undefined,
): string | null {
  if (!data) return null;
  if (data.status !== "ok") return null;
  return data.last_sync ?? null;
}
```

- [ ] **Step 4: Run to verify pass**

Run: `cd web && npm test -- usePerformance.test.ts`

Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
git add web/lib/usePerformance.ts web/tests/usePerformance.test.ts
git commit -m "feat(web): usePerformance broker-aware, union-aware extractTimestamp"
```

### Task 5.3: `performanceChart.ts` gates on `status === "ok"`

**Files:**

- Modify: `web/lib/performanceChart.ts`

- [ ] **Step 1: Find every read of `summary.starting_equity` / `summary.ending_equity`**

Run: `grep -n "summary\." web/lib/performanceChart.ts`

- [ ] **Step 2: Replace each call site with a `status === "ok"` gate**

Example: a function `buildEquityChart(data: PerformanceData)` that previously did `const start = data.summary.starting_equity` becomes:

```typescript
export function buildEquityChart(data: PerformanceData) {
  if (data.status !== "ok") return { lines: [], yDomain: [0, 0] };
  const start = data.summary.starting_equity;
  // skip null benchmark points so the chart doesn't NaN out
  const points = data.series.filter(p => p.benchmark_close !== null);
  ...
}
```

Apply the same gate to every function in the file.

- [ ] **Step 3: Typecheck**

Run: `cd web && npm run typecheck`

Expected: previously-red errors in this file are gone.

- [ ] **Step 4: Commit**

```bash
git add web/lib/performanceChart.ts
git commit -m "fix(web): performanceChart gates on status, skips null benchmark points"
```

### Task 5.4: `PerformancePanel.tsx` — branching + low-confidence badge

**Files:**

- Modify: `web/components/PerformancePanel.tsx`
- Test: `web/tests/PerformancePanel.test.tsx`

- [ ] **Step 1: Write the test up front (key states)**

`web/tests/PerformancePanel.test.tsx`:

```typescript
import { render, screen } from "@testing-library/react";
import { PerformancePanel } from "@/components/PerformancePanel";

const okSummary = {
  starting_equity: 100, ending_equity: 110, pnl: 10, total_return: 0.1,
  trading_days: 30, max_drawdown: -0.05, max_drawdown_duration_days: 2,
  current_drawdown: -0.01,
  low_confidence: true, sharpe_se: 2.9, sortino_se: 2.9,
  sharpe_ratio: 1.2, sortino_ratio: 1.4, calmar_ratio: 1.0,
  annualized_return: 0.5, annualized_volatility: 0.2, downside_deviation: 0.1,
  var_95: -0.03, cvar_95: -0.05, tail_ratio: 1.1, ulcer_index: 0.02,
  beta: 1.0, alpha: 0.0, correlation: 0.5, r_squared: 0.25,
  tracking_error: 0.1, information_ratio: 0.5, treynor_ratio: 1.0,
  upside_capture: 1.1, downside_capture: 0.9,
  hit_rate: 0.5, positive_days: 15, negative_days: 14, flat_days: 1,
  best_day: 0.05, worst_day: -0.04, average_up_day: 0.02, average_down_day: -0.02,
  win_loss_ratio: 1.1, skew: 0.1, kurtosis: 3.0,
};

it("renders low-confidence badge + SE tooltip when low_confidence=true", () => {
  const data = {
    status: "ok" as const, as_of: "2026-06-01", last_sync: "2026-06-01",
    period_start: "2026-01-01", period_end: "2026-06-01", period_label: "YTD NAV Change",
    scope: { broker: "IB" as const, account_env: "live", broker_account: "DU" },
    currency: "USD" as const, benchmark: "SPY" as const, benchmark_total_return: 0.1,
    trades_source: "nav_history" as const, methodology: {} as any, price_sources: {} as any,
    summary: okSummary,
    series: [], warnings: [], contracts_missing_history: [],
  };
  render(<PerformancePanel data={data} activeAccount="ib" />);
  const badges = screen.getAllByTestId("low-confidence-badge");
  expect(badges.length).toBeGreaterThan(0);
  const tip = screen.getByTestId("sharpe-tooltip");
  expect(tip.textContent).toMatch(/SE.*2\.9/);
});

it("does NOT render low-confidence badge when low_confidence=false", () => {
  const data = {
    status: "ok" as const, as_of: "2026-06-01", last_sync: "2026-06-01",
    period_start: "2026-01-01", period_end: "2026-06-01", period_label: "YTD",
    scope: { broker: "IB" as const, account_env: "live", broker_account: "DU" },
    currency: "USD" as const, benchmark: "SPY" as const, benchmark_total_return: 0.1,
    trades_source: "nav_history" as const, methodology: {} as any, price_sources: {} as any,
    summary: { ...okSummary, low_confidence: false, sharpe_se: null, sortino_se: null },
    series: [], warnings: [], contracts_missing_history: [],
  };
  render(<PerformancePanel data={data} activeAccount="ib" />);
  expect(screen.queryAllByTestId("low-confidence-badge")).toHaveLength(0);
});

it("renders futu_not_synced CTA when status=insufficient", () => {
  render(<PerformancePanel data={{
    status: "insufficient_history" as const, reason: "futu_not_synced" as const,
    days_collected: 0, days_required_for_curve: 5, days_required_for_metrics: 30,
    inception_date: null, hero_net_liq: 224683.0, currency: "USD" as const,
  }} activeAccount="futu" />);
  expect(screen.getByText(/Sync Futu/)).toBeInTheDocument();
});
```

- [ ] **Step 2: Update `PerformancePanel.tsx`**

At the top of the component, branch on `data.status` BEFORE any `data.summary.*` read:

```typescript
export function PerformancePanel({ data, activeAccount }: Props) {
  if (data.status === "insufficient_history") {
    if (data.reason === "futu_not_synced") return <FutuSyncCta hero={data.hero_net_liq} />;
    if (data.reason === "scope_unset") return <ScopeUnsetState />;
    return <CollectingHistoryState days={data.days_collected} />;
  }
  const { summary } = data;
  const fmtPct = (v: number | null) => v === null ? "---" : `${(v * 100).toFixed(2)}%`;
  const fmtRatio = (v: number | null) => v === null ? "---" : v.toFixed(2);
  // ...existing render with summary fields...
  // Hero
  <h1>{fmtCurrency(summary.ending_equity)} <span className="unit">{data.currency}</span></h1>
  // Risk metric card example (apply the same pattern to every annualized risk card)
  <Card>
    <Label>Sharpe</Label>
    <Value>{fmtRatio(summary.sharpe_ratio)}</Value>
    {summary.low_confidence && summary.sharpe_se !== null && (
      <Badge data-testid="low-confidence-badge"
             title={`Low-confidence: based on ${summary.trading_days} sessions; SE ≈ ${summary.sharpe_se.toFixed(2)}`}
             data-testid-tip="sharpe-tooltip">LOW CONF</Badge>
    )}
  </Card>
  // ...
  // Warnings
  {data.warnings.map(w => <Warning key={w}>{w}</Warning>)}
}
```

Repeat the low-confidence badge wrapper on Sortino, Calmar, Sharpe-derived ratios, Beta, Alpha, IR, Capture, Treynor, VaR, CVaR. (Distribution stats — hit_rate, best_day, worst_day, skew, kurtosis — do not get the badge; they are non-annualized.)

- [ ] **Step 3: Run tests**

Run: `cd web && npm test -- PerformancePanel.test.tsx`

Expected: 3 PASS.

- [ ] **Step 4: Commit**

```bash
git add web/components/PerformancePanel.tsx web/tests/PerformancePanel.test.tsx
git commit -m "feat(web): PerformancePanel branches on status, renders low-confidence"
```

### Task 5.5: `WorkspaceSections.tsx` forwards `activeAccount`

**Files:**

- Modify: `web/components/WorkspaceSections.tsx`

- [ ] **Step 1: One-line change**

Find the `<PerformancePanel ... />` call site and add the `activeAccount={activeAccount}` prop. Verify the parent has `activeAccount` in scope (it does — this is the account switcher's state).

- [ ] **Step 2: Typecheck**

Run: `cd web && npm run typecheck`

Expected: no new errors.

- [ ] **Step 3: Commit**

```bash
git add web/components/WorkspaceSections.tsx
git commit -m "feat(web): forward activeAccount to PerformancePanel"
```

### Task 5.6: `/api/performance/route.ts` broker proxy

**Files:**

- Modify: `web/app/api/performance/route.ts`

- [ ] **Step 1: Replace body**

```typescript
import { NextResponse } from "next/server";
import { xenonFetch } from "@/lib/server/xenonFetch";

export const dynamic = "force-dynamic";

export async function GET(req: Request) {
  const url = new URL(req.url);
  const broker = url.searchParams.get("broker") ?? "IB";
  const data = await xenonFetch(
    `/performance?broker=${encodeURIComponent(broker)}`,
  );
  return NextResponse.json(data);
}
```

Drop any in-process cache logic that was there.

- [ ] **Step 2: Commit**

```bash
git add web/app/api/performance/route.ts
git commit -m "feat(web): performance route proxies broker query param"
```

---

## Phase 6 — Cleanup

### Task 6.1: Delete retired tests

**Files:** delete `scripts/tests/test_portfolio_performance.py`, `scripts/tests/test_performance_lock.py`

- [ ] **Step 1: Confirm they really test retired behaviour**

Run: `grep -l "xenon-portfolio-perf\|POST.*performance" scripts/tests/test_portfolio_performance.py scripts/tests/test_performance_lock.py`

If they reference the old subprocess or POST-dedup logic, proceed.

- [ ] **Step 2: Delete**

```bash
git rm scripts/tests/test_portfolio_performance.py scripts/tests/test_performance_lock.py
```

- [ ] **Step 3: Commit**

```bash
git commit -m "chore(tests): remove retired portfolio_performance + performance_lock"
```

### Task 6.2: Deprecation warnings on retired CLIs

**Files:**

- Modify: `src/xenon/reports/portfolio_performance.py`
- Modify: `src/xenon/reports/performance_explainer_report.py`

- [ ] **Step 1: Add import-time `DeprecationWarning`**

At the top of each file (after stdlib imports):

```python
import warnings
warnings.warn(
    f"{__name__} is deprecated — use FastAPI GET /performance instead. "
    "Removal target: next release.",
    DeprecationWarning,
    stacklevel=2,
)
```

And in `main()` (or the CLI entry point), print a banner before any work and `return 0`:

```python
def main(argv=None):
    print(
        "xenon-portfolio-perf is deprecated; the FastAPI service at "
        "/performance is now the source of truth. Run `xenon-api` and "
        "GET /performance?broker=IB|FUTU. This CLI will be removed in a "
        "future release.",
        file=sys.stderr,
    )
    return 0
```

(Same shape for `performance_explainer_report.py::main`.)

- [ ] **Step 2: Commit**

```bash
git add src/xenon/reports/portfolio_performance.py \
        src/xenon/reports/performance_explainer_report.py
git commit -m "chore(perf): deprecate retired CLIs"
```

---

## Phase 7 — Browser & E2E verification (per `web/CLAUDE.md`)

### Task 7.1: `performance-broker-switch.spec.ts`

**Files:**

- Create: `web/e2e/performance-broker-switch.spec.ts`

- [ ] **Step 1: Write the spec**

```typescript
import { test, expect } from "@playwright/test";

test("IB and Futu tabs render different hero numbers", async ({ page }) => {
  await page.goto("/performance");
  const ibHero = await page
    .locator("[data-testid='performance-hero']")
    .textContent();
  await page.click("[data-testid='account-tab-futu']");
  await page.waitForResponse((r) =>
    r.url().includes("/api/performance?broker=FUTU"),
  );
  const futuHero = await page
    .locator("[data-testid='performance-hero']")
    .textContent();
  expect(ibHero).not.toEqual(futuHero);
});
```

- [ ] **Step 2: Run E2E**

Run: `cd web && npx playwright test performance-broker-switch.spec.ts`

Expected: PASS (requires dev server + FastAPI + IB Gateway up; see Startup Checklist in root CLAUDE.md).

- [ ] **Step 3: Commit**

```bash
git add web/e2e/performance-broker-switch.spec.ts
git commit -m "test(e2e): performance broker switch"
```

### Task 7.2: `performance-futu-cold-start.spec.ts`

- [ ] **Step 1: Spec**

```typescript
import { test, expect } from "@playwright/test";

test("Futu cold-start surfaces sync CTA, then unlocks after sync", async ({
  page,
}) => {
  // Pre-condition: NO futu nav_history rows (test harness should truncate before run).
  await page.goto("/performance");
  await page.click("[data-testid='account-tab-futu']");
  await expect(
    page.locator("text=Sync Futu to start collecting history"),
  ).toBeVisible();
  await page.click("[data-testid='futu-sync-cta']");
  await page.waitForResponse((r) => r.url().includes("/futu/sync") && r.ok());
  // With only 1 day, status stays insufficient_history reason=collecting until threshold.
  await expect(page.locator("text=COLLECTING HISTORY")).toBeVisible();
});
```

- [ ] **Step 2: Run + commit**

```bash
cd web && npx playwright test performance-futu-cold-start.spec.ts
git add web/e2e/performance-futu-cold-start.spec.ts
git commit -m "test(e2e): performance futu cold-start"
```

### Task 7.3: Manual browser verification

- [ ] **Step 1: Start dev stack**

```bash
scripts/infra/dev.sh paper
```

- [ ] **Step 2: Open `http://localhost:3000/performance`**

Verify in browser:

- IB tab → curve + summary number that matches `xenon.nav_history` for the IB scope.
- Click Futu tab → curve **changes** (or `Sync Futu` CTA appears if no FUTU rows yet).
- Click `Sync Futu` CTA, wait, switch tabs back, observe collecting-history state.
- Switch back to IB → curve restores (no flash of FUTU data).
- Hover any annualized risk metric when n < 126 → low-confidence badge tooltip shows SE.

- [ ] **Step 3: Capture screenshots**

```bash
# from chrome-cdp or playwright
# save as performance-ib-final.png, performance-futu-cta.png, performance-futu-syncing.png
```

- [ ] **Step 4: Commit screenshots if a follow-up review needs them, or delete**

If keeping: add them to `.playwright-mcp/` (gitignored) and reference in PR description. Otherwise delete.

---

## Final checks (before opening the PR)

- [ ] `uv run pytest` — full Python suite green
- [ ] `cd web && npm test` — full Vitest green
- [ ] `cd web && npm run typecheck && npm run lint` — green
- [ ] `cd web && npx playwright test` — both new E2E specs green
- [ ] Phase 0 verification artifact committed and `XENON_IB_DAILYPNL_INCLUDES_CASHFLOWS` default set correctly in `.env.example`
- [ ] PR body links the spec (`docs/superpowers/specs/2026-05-31-performance-rebuild-design.md`) and the verification artifact
- [ ] Rollback note in PR body: "Migrations are additive; forward-only deploy is safe; revert PR to roll back."
