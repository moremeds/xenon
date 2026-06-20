# Xenon Codebase Review — 2026-04-24

Opinionated, evidence-based review. All findings cite `path:line`. No fixes proposed; this is a defects-and-risk document, not a PR plan.

## Executive Summary — Top 10 Findings

| #   | Finding                                                                                                                                                                                                                                                                                                                                                                                         | Severity | Pointer                                                                                           |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------------------------- |
| 1   | `trade_log.json` has **5 concurrent writers with no lock** (ib_execute, ib_reconcile, ib_sync, fill_monitor, exit_orders) — plain `open("w") + json.dump()`, no atomic rename. Corruption inevitable under concurrent fills.                                                                                                                                                                    | P0       | `src/xenon/execution/ib_execute.py:287-343` + 4 others                                            |
| 2   | Market-hours holiday gap: `is_market_open()` is weekend-only; US market holidays are not gated. UW budget bleeds ~8–10 holidays/yr × ~2k auto-refresh calls ≈ 16–20k calls/yr wasted against the 20k/day cap.                                                                                                                                                                                   | P1       | `src/xenon/utils/market_hours.py:7`; gate at `src/xenon/api/services/uw_analyze_cache.py:650-668` |
| 3   | **`WorkspaceSections.tsx` is 4,669 LOC** — one component holding order form, portfolio view, regime logic, share cards. Tooling slows, tests cannot isolate, change risk on every edit.                                                                                                                                                                                                         | P0       | `web/components/WorkspaceSections.tsx`                                                            |
| 4   | `optionCloseCache` on the Node realtime bridge is an **uncapped persistent `Map`**, 2026-04-22 memory-plan flagged; still unfixed. Grows with every distinct option contract ever seen and is persisted to disk.                                                                                                                                                                                | P1       | `scripts/infra/ib_realtime/ib_realtime_server.js:431`                                             |
| 5   | `uw_analyze_history/` is **358 MB across 37,114 files** — the cache's own code comments say "Retention: none in v1" and warns ~500k files is the latency cliff. Current projection hits that threshold within ~9 months.                                                                                                                                                                        | P1       | `src/xenon/api/services/uw_analyze_cache.py:403-407`; directory size verified (358M/37114 files)  |
| 6   | **~1,100 ruff violations** (490 W293, 260 F401, 131 I001, 91 E402…) and no CI enforcement of the ruleset. Unused imports + out-of-order imports mask actual regressions.                                                                                                                                                                                                                        | P1       | `ruff check .` statistics                                                                         |
| 7   | `naked_short_audit.py` **imports `xenon.api.ib_pool`** — execution layer reaches into the API layer. Classic layer inversion; makes the audit non-testable without booting the API pool.                                                                                                                                                                                                        | P1       | `src/xenon/execution/naked_short_audit.py:19`                                                     |
| 8   | **TypeScript build has 153 tsc errors**, nearly all in tests. Test fixtures use untyped `Record<string, unknown>` shims, `: any` casts in hot-path tests (`use-prices-ws-stability.test.ts:173,354,356,477`). `tsc --noEmit` is not wired to the test pipeline.                                                                                                                                 | P1       | `web/tests/**`                                                                                    |
| 9   | **15+ web tests assert `toHaveBeenCalled()` with no arg/payload check** (`position-order-modal.test.tsx:103,130,212`, `orders-place-quote-tokens-passthrough.test.ts:60`, etc.). Would not catch BUY↔SELL reversal, missing Bearer header, or wrong endpoint. Matches the failure mode of `feedback_live_e2e_surfaces_contract_bugs.md`.                                                        | P1       | see §1 table                                                                                      |
| 10  | Memory `project_scripts_reorg_phase1_shipped.md` is **stale** — the verb-first `scripts/scanners/` `scripts/fetchers/` `scripts/execution/` buckets it describes no longer exist. Current `scripts/` holds only `infra/ lib/ migrations/ services/ ta_lib/ tests/`; modules moved entirely under `src/xenon/`. This also makes the `feedback_shim_vs_real_patching.md` warning mostly obsolete. | P1       | `/bin/ls scripts/` vs memory claim                                                                |

---

## 1. Code Quality & Test Effectiveness

### 1.1 Python — coverage map

| Critical surface                                           | Has test that would fail on regression? | Evidence                                                                                                          |
| ---------------------------------------------------------- | --------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Order placement routing (`/orders/place` → preflight → IB) | ✅ Strong                               | `src/xenon/api/tests/test_preflight_route.py` — SPX block, spoofed-multiplier=1 bypass, insufficient-shares       |
| Cancel/modify failure classification (503/409/404/4xx)     | ✅ Strong                               | `src/xenon/api/tests/test_orders_routes_failures.py` — 50 tests mapping subprocess exit → HTTP status             |
| Naked-short audit                                          | ✅ Strong                               | `scripts/tests/test_naked_short_audit.py` (ratio/Jade Lizard/vertical permutations)                               |
| Quote-token / band / tick-grid                             | ✅ Strong                               | `scripts/tests/test_quote_guard.py`, `scripts/tests/test_quote_guard_combo.py`                                    |
| Scanner scoring                                            | ✅ Strong                               | `scripts/tests/test_scanner_lib_scoring.py` — pure unit, no mocks                                                 |
| Scripts-reorg trend catalyst path                          | ✅ Strong                               | `scripts/tests/test_trend_scan_catalysts.py`                                                                      |
| Kelly sizing enforced at place route                       | ⚠️ Partial                              | Unit tests on Kelly formula; **no test** ties Kelly → order quantity cap at `/orders/place`                       |
| place-order → fill → reconcile                             | ⚠️ Partial                              | `scripts/tests/test_ib_reconcile.py` exists; no end-to-end "placed → filled → trade_log updated" simulation       |
| IB gateway reconnection under drop                         | ❌ Mocked only                          | `scripts/tests/test_ib_resilient.py` mocks IB; no test harness runs the real gateway lifecycle                    |
| **Futu client adapter**                                    | ❌ None                                 | No `test_futu_client.py`. The read-only silent-degrade contract (root CLAUDE.md) is uncovered.                    |
| Clerk auth localhost bypass                                | ✅ Partial                              | `src/xenon/api/tests/test_auth.py:65-91` — localhost skip tested on HTTP; no test covers WS ticket localhost skip |

**Coverage commands to run for real numbers** (I did not run these — too slow in-session):

```bash
python3.13 -m pytest scripts/tests/ src/xenon/api/tests/ --cov=src/xenon --cov-report=term-missing
# Expected: headline number hides the pattern — IB/Futu adapters 0%, orders_store 80-90%, scanners 60-70%.
```

### 1.2 Python — ruff / hygiene

Ran `ruff check .` — headline counts (top violations only):

| Rule                            | Count | Notes                                                                                    |
| ------------------------------- | ----- | ---------------------------------------------------------------------------------------- |
| W293 blank-line-with-whitespace | 490   | Pure cosmetic but blocks automated format checks                                         |
| F401 unused-import              | 260   | Masks dead code; also signal that modules moved but imports not cleaned                  |
| I001 import-order               | 131   | `isort` not enforced                                                                     |
| E402 module-import-not-at-top   | 91    | Typical for conditional imports in CLI entrypoints — often legitimate but worth auditing |
| F541 f-string-no-placeholder    | 76    | Harmless but sloppy                                                                      |
| E741 ambiguous-variable-name    | 60    | `l`, `O`, `I` — real risk in numeric code                                                |
| F841 unused-variable            | 53    | Low-value dead code                                                                      |
| B904 raise-without-from         | 23    | Loses exception chain in production logs                                                 |
| E712 true-false-comparison      | 21    | Signals misunderstanding of `None`/truthiness                                            |

| Finding                                                                                                                                                                                                             | File:line                                     | Severity             |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------- | -------------------- |
| No `ruff` / `mypy` gate in CI (search `.github/workflows` for either)                                                                                                                                               | root                                          | P1                   |
| `from xenon.api.ib_pool import ClientIdBusy, acquire_owner` inside execution layer                                                                                                                                  | `src/xenon/execution/naked_short_audit.py:19` | P1 — layer inversion |
| Dead-code sweeper flagged **173 items** (hook output at session start) — almost entirely `__pycache__` + stale sibling scripts. Not architectural damage, but adds noise and breaks grep hit ratios.                | session hook stdout                           | P2                   |
| Scripts reorg claim in memory is stale: there is no `scripts/scanners/`, `scripts/fetchers/`, `scripts/execution/` today — those live at `src/xenon/…`. Shim-patching feedback memory is therefore mostly obsolete. | `/bin/ls scripts/`                            | P1 (for memory)      |

### 1.3 Python — ceremonial vs meaningful tests

Looked for the failure mode from `feedback_live_e2e_surfaces_contract_bugs.md` (mocked-boundary tests that never exercise a real cross-process contract).

| File:line                                                  | Anti-pattern                                                                                                                                              | Notes                                                                      |
| ---------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `src/xenon/api/tests/test_orders_routes_failures.py:48-52` | `_patch_runner` lambda returns a pre-built `ScriptResult`; tests assert payload shape but never exercise subprocess stdout parser on a real non-zero exit | This is exactly the class of test that missed the PR-C/D F5 contract break |
| `scripts/tests/test_pool_order_manage.py:41-54`            | Asserts `result["status"] == "ok"` but never inspects `orders_store` state after cancel                                                                   | Cancel could fail silently with status=ok                                  |
| `scripts/tests/test_ib_order_manage.py:69-82`              | Mocks set `trade.orderStatus.status = "Cancelled"` and assert `exc.value.code == 0` — no reconciliation check                                             | Mutating a mock is not a test of the real reconciler                       |
| `scripts/tests/test_uw_analyze_route.py:46-150`            | Rebuilds `AnalysisReport` fixtures per test (~100 lines); each test exercises the same happy path differently mocked                                      | High surface-area tests that verify fixture plumbing, not behavior         |

**Positive examples (keep):** `test_scanner_lib_scoring.py`, `test_quote_guard.py`, `test_preflight_route.py`, `test_trend_scan_catalysts.py`. These assert real outputs on real inputs.

### 1.4 TypeScript — coverage map

| Surface                                          | Test that would fail on regression? | Evidence                                                                                                                |
| ------------------------------------------------ | ----------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Naked-short guard (21 cases including 1×2 ratio) | ✅ Strong                           | `web/tests/naked-short-guard.test.ts:97-301`                                                                            |
| Exposure delta sign (SHORT call = –)             | ✅ Strong                           | `web/tests/exposure-breakdown.test.ts:42-86`                                                                            |
| useSyncHook cache-first → active sync            | ✅ Strong                           | `web/tests/use-sync-hook-inactive-load.test.ts:29-78`                                                                   |
| xenonApi error detail preservation               | ✅ Strong                           | `web/tests/xenon-api.test.ts:42-227`                                                                                    |
| **Bearer token attached by xenonFetch**          | ❌ None                             | `xenonApi.ts:28` adds header; `xenon-api.test.ts:69-84` tests Content-Type but never `Authorization`                    |
| **Futu POST-only sync (not GET stale)**          | ❌ None                             | Hook test does not assert `method: 'POST'`                                                                              |
| Combo BAG price uses cross-field                 | ⚠️ Implicit                         | `order-reliability.test.ts` references `computeNetOptionQuote()`; no explicit bid/ask-cross-field assertion vs. mid-mid |
| ModifyOrderModal BAG leg sign preservation       | ❌ None                             | `open-order-combo-modify.test.ts:6-73` builds target but does not assert leg `action` preserved                         |
| uw-analyze SSE progressive merge                 | ⚠️ Partial                          | Route tests cover status codes; no explicit "cache-first, then SSE merge, then last-known-good overlay" test            |

### 1.5 TypeScript — ceremonial vs meaningful

Ten concrete anti-patterns (there are more):

| File:line                                                    | Anti-pattern                                                                        |
| ------------------------------------------------------------ | ----------------------------------------------------------------------------------- |
| `web/tests/position-order-modal.test.tsx:103`                | `waitFor(() => expect(placeMock).toHaveBeenCalled())` — no body/method check        |
| `web/tests/position-order-modal.test.tsx:130`                | duplicate of the above                                                              |
| `web/tests/position-order-modal.test.tsx:212`                | duplicate of the above                                                              |
| `web/tests/orders-place-quote-tokens-passthrough.test.ts:60` | `expect(xenonApi.xenonFetch).toHaveBeenCalled()` — no endpoint or payload assertion |
| `web/tests/api-routes-extended.test.ts:308`                  | `expect(mockWriteFile).toHaveBeenCalled()` — no path/content check                  |
| `web/tests/modify-order-combo-routing.test.tsx:88`           | `expect(onConfirm).toHaveBeenCalled()` — no payload check                           |
| `web/tests/chat-advanced.test.ts:300`                        | `expect(setMessages).toHaveBeenCalled()` — no message asserted                      |
| `web/tests/api-routes.test.ts:39`                            | Global `vi.mock("@/lib/xenonApi")` — 50+ tests verify the mock, not the SUT         |
| `web/tests/uw-analyze.route.test.ts:7-12`                    | Mocks `xenonFetch` entirely; real client behavior never exercised                   |
| `web/tests/performance-route.test.ts:12`                     | Mocks xenonApi layer; route tests cannot fail on Bearer/endpoint bugs               |
| `web/tests/use-prices-ws-stability.test.ts:267`              | `expect(spy).toHaveBeenCalled()` — no call count                                    |

**Pattern:** assertion discipline collapses once tests start mocking `xenonApi`. The tests prove the mock fired; they prove nothing about the wire.

### 1.6 TypeScript — hygiene

| Metric                                     | Count / Top offenders                                                                                                                                                      |
| ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `: any` in source (excluding node_modules) | 7 — all in tests: `use-prices-ws-stability.test.ts:173,354,356,477`, `position-order-modal.test.tsx:36`, `position-order-modal-quote-tokens.test.tsx:122,129`              |
| Non-null `!` in components                 | 8 live sites: `PortfolioByStructure.tsx:122`, `PositionTable.tsx:576`, `CriHistoryChart.tsx:328`, `MetricCards.tsx:408`, `CtaTables.tsx:169`, `WorkspaceSections.tsx:2410` |
| Unchecked `as` casts                       | ~80 across `web/**` — most are `JSON.parse(x) as Type` (acceptable at boundaries) or test-helper casts (acceptable)                                                        |
| Components > 300 LOC                       | `WorkspaceSections.tsx` 4669 · `OrderTab.tsx` 1244 · `OptionsChainTab.tsx` 1109 · `MetricCards.tsx` 953 · `GexPanel.tsx` 901 · `PositionTable.tsx` 825                     |
| `tsc --noEmit` errors                      | 153 (mostly TS7053 and TS2322 in test fixtures)                                                                                                                            |

### 1.7 Findings summary (P-ranked)

| #   | Area                                                               | File:line                                                     | Severity | Evidence                                                                                     |
| --- | ------------------------------------------------------------------ | ------------------------------------------------------------- | -------- | -------------------------------------------------------------------------------------------- |
| 1   | Trade log concurrent writers                                       | `src/xenon/execution/ib_execute.py:287-343` + 4 other writers | P0       | §3 table; no lock/atomic                                                                     |
| 2   | WorkspaceSections god-component                                    | `web/components/WorkspaceSections.tsx`                        | P0       | 4669 LOC                                                                                     |
| 3   | `vi.mock("@/lib/xenonApi")` pattern spreads across 50+ route tests | `web/tests/api-routes.test.ts:39`                             | P1       | SUT is mocked out                                                                            |
| 4   | Naked-short audit layer inversion                                  | `src/xenon/execution/naked_short_audit.py:19`                 | P1       | execution → api                                                                              |
| 5   | No Bearer header test                                              | `web/tests/xenon-api.test.ts:69-84`                           | P1       | CLERK_SECRET integration untested                                                            |
| 6   | Futu method untested                                               | `web/lib/futuPortfolioAdapter.ts` + hook tests                | P1       | GET-returns-stale regression cannot be caught by test                                        |
| 7   | Futu client has zero Python unit tests                             | `src/xenon/clients/futu_client.py`                            | P1       | root CLAUDE.md silent-degrade contract uncovered                                             |
| 8   | Kelly → order-size gate untested end-to-end                        | `src/xenon/api/routes/*` (place route)                        | P1       | Gate 3 in Four Gates                                                                         |
| 9   | Combo mid-mid vs cross-field not explicitly asserted               | `web/tests/order-reliability.test.ts`                         | P1       | see web/CLAUDE.md "CRITICAL" rule                                                            |
| 10  | Large components make client/server split unassessable             | 6 files >800 LOC                                              | P1       | contain both render and effect logic                                                         |
| 11  | 153 tsc errors; no pre-commit gate                                 | `web/`                                                        | P1       | `tsc --noEmit`                                                                               |
| 12  | ~1,100 ruff violations; no CI gate                                 | root                                                          | P1       | `ruff check . --statistics`                                                                  |
| 13  | 50 `: Any` type hints in `src/xenon/`                              | broad                                                         | P2       | reduces mypy value                                                                           |
| 14  | WS ticket validation: no concurrent-access test                    | `src/xenon/api/ws_ticket.py`                                  | P2       | `_cleanup_expired` race plausible                                                            |
| 15  | No single E2E test spanning subprocess stdout/exit-code boundary   | —                                                             | P1       | the contract break from `feedback_live_e2e_surfaces_contract_bugs.md` would recur undetected |

---

## 2. Performance

### 2.1 Validation of the 2026-04-22 memory-plan

All six hotspots remain present and unfixed as of 2026-04-24.

| Claim in plan                                            | Verified?    | Path:line                                                                                                     |
| -------------------------------------------------------- | ------------ | ------------------------------------------------------------------------------------------------------------- |
| `uw_analyze_cache` live cache bounded, archive unbounded | ✅ Unchanged | `src/xenon/api/services/uw_analyze_cache.py:52` (`_MAX_ENTRIES=300`), lines 403-407 ("Retention: none in v1") |
| `optionCloseCache` uncapped `Map`, persisted to disk     | ✅ Unchanged | `scripts/infra/ib_realtime/ib_realtime_server.js:431`                                                         |
| `_syncCache` module-level, no TTL                        | ✅ Unchanged | `web/lib/useSyncHook.ts:13`                                                                                   |
| `useChainPrefetch` per-ticker Map, no per-expiry TTL     | ✅ Unchanged | `web/lib/useChainPrefetch.ts:24`                                                                              |
| `usePrices.prices/fundamentals` unbounded records        | ✅ Unchanged | `web/lib/usePrices.ts:91-92`                                                                                  |
| `portfolio_performance.py` full-DF materialization       | ✅ Unchanged | lines 534, 588, 859                                                                                           |

### 2.2 UW budget — holiday gap is real

`src/xenon/api/services/uw_analyze_cache.py:650-668` gates automatic refresh on `self._market_open_fn()`. That function is `xenon.utils.market_hours:is_market_open()`, whose own docstring (`src/xenon/utils/market_hours.py:7`) says "Does not account for market holidays (simplified)." Although a holiday dataset exists at `xenon.utils.market_calendar`, it is not consulted by `is_market_open()`. On market holidays the closed-market gate treats the day as OPEN. Real impact: the automatic polling in `web/lib/useUwAnalyze.ts` plus the sidebar polling issues refreshes, and the 20k/day cap bleeds 500–2,000 calls on each US holiday (~9–10/yr).

**Minimum-effort fix locus (not proposed, only localized):** `src/xenon/utils/market_hours.py:is_market_open` + a call site change is a one-file diff.

### 2.3 Findings — performance

| #   | Area                                                                                                                                                                                                    | File:line                                                                                | Severity | Issue                                                                           | Estimated win                       | Cost |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | -------- | ------------------------------------------------------------------------------- | ----------------------------------- | ---- |
| 1   | `optionCloseCache` uncapped persistent Map                                                                                                                                                              | `ib_realtime_server.js:431`                                                              | P0       | Grows forever across restarts; every option contract ever seen is retained      | 5–50 MB Node RSS + JSON file bloat  | S    |
| 2   | `uw_analyze_history/` unbounded (37,114 files, 358 MB today)                                                                                                                                            | `uw_analyze_cache.py:403-407` (doc-only caveat); `uw_analyze_cache.py:_archive_snapshot` | P1       | Latency cliff at ~500k files; 1.5k/day projects ~9 months                       | 100–400 MB disk + p99 I/O           | S    |
| 3   | Holiday gap lets UW auto-refresh fire on holidays                                                                                                                                                       | `market_hours.py:7`; gate at `uw_analyze_cache.py:650-668`                               | P1       | ~1k calls/holiday × 9 holidays                                                  | ~9k calls/yr + sidebar widget drift | S    |
| 4   | `_syncCache` has no TTL or size cap                                                                                                                                                                     | `useSyncHook.ts:13`                                                                      | P1       | Browser tab memory on long sessions; stale large payloads                       | 10–20 MB per long session           | S    |
| 5   | `prices`/`fundamentals` objects unbounded + drive cascade re-renders                                                                                                                                    | `usePrices.ts:91-92`                                                                     | P1       | 100-symbol watchlist allocates 100 PriceData on every diff                      | 5–30 MB + CPU churn                 | M    |
| 6   | `portfolio_performance.py` DataFrame materialization                                                                                                                                                    | `portfolio_performance.py:534,588,859`                                                   | P1       | Full marks history loaded into dicts + several DFs coexist                      | 50–200 MB peak                      | M    |
| 7   | `evaluate.py` keeps all milestone raw payloads in-memory until final gate                                                                                                                               | `src/xenon/reports/evaluate.py` (module structure)                                       | P2       | 7 milestones × N tickers of intermediate state                                  | 20–50 MB peak                       | M    |
| 8   | `/futu/sync` silent-degrade path: generic `Exception` catch, returns cached or `None` — operationally fine but there's no observability signal when OpenD is down                                       | `src/xenon/clients/futu_client.py:287`                                                   | P2       | silent-success is invisible until manual check                                  | — (observability only)              | S    |
| 9   | Next.js `fetch` policy is inconsistent: external sources use `cache: "no-store"`, but shared expensive endpoints (`/api/portfolio`, `/api/uw-analyze/portfolio`) don't advertise `next: { revalidate }` | `web/app/api/**/route.ts`                                                                | P2       | Missed SWR wins                                                                 | 2–5s on some routes                 | S    |
| 10  | IB realtime reconnect is healthy (5s backoff, 45s stale-tick watchdog)                                                                                                                                  | `ib_realtime_server.js:1256-1274, 1538-1551`                                             | —        | No action                                                                       | —                                   | —    |
| 11  | Per-ticker asyncio.Lock dict inside `UwAnalyzeCache` is never pruned alongside entry eviction                                                                                                           | `uw_analyze_cache.py:47-59`                                                              | P2       | Lock dict grows with unique tickers ever queried; RSS-past-7GB incident context | modest long-run                     | S    |
| 12  | `data/ta.duckdb` is 84 MB with no active read concurrency control; OK today because only GitHub Action writes                                                                                           | `ta_lib/parquet_store.py` + action                                                       | P2       | Schema drift if action evolves ahead of reader                                  | —                                   | S    |

### 2.4 Profiling targets — exact commands

```bash
# 1) portfolio_performance.build_payload peak memory (largest Python RSS candidate)
py-spy record -o /tmp/portfolio_perf.svg -- \
    python3.13 -m xenon.reports.portfolio_performance --out /tmp/report.html

# 2) evaluate.py parallel milestones (fan-out for small universes)
py-spy record -o /tmp/evaluate.svg -- \
    python3.13 -m xenon.reports.evaluate AAPL MSFT TSLA NVDA

# 3) UW cache concurrent refresh (look for lock contention + diff serialization cost)
py-spy record -o /tmp/uw_refresh.svg -- \
    python3.13 -c "import asyncio; from xenon.api.services.uw_analyze_cache import UwAnalyzeCache; \
    c=UwAnalyzeCache(); asyncio.run(c.refresh_portfolio())"

# 4) Trend scan wall-clock budget
py-spy record -o /tmp/trend_scan.svg -- \
    python3.13 -m xenon.scanners.trend.scan --top 25

# 5) Node realtime server: Chrome DevTools heap snapshot after 1h steady state
#   - open chrome://inspect (Node inspector) or start with --inspect-brk
#   - take 3 snapshots 5 min apart; look for optionCloseCache and symbolStates growth
node --inspect scripts/infra/ib_realtime/ib_realtime_server.js
```

For browser memory: use Chrome DevTools Memory tab on `/portfolio` after 30 min of usage, diff heap snapshots — expect growth to cluster in `_syncCache` entries and `prices/fundamentals` keyed objects.

---

## 3. Persistence — inventory & proposal

### 3.1 Inventory

| Path                                                                                                                                                                                         | Type                | Class        | Writer(s)                                                        | Size                     | Concern                                                                                              |
| -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------- | ------------ | ---------------------------------------------------------------- | ------------------------ | ---------------------------------------------------------------------------------------------------- |
| `data/orders.duckdb`                                                                                                                                                                         | DuckDB              | operational  | `src/xenon/execution/orders_store.py`                            | 268 K                    | `_WRITE_LOCK` + UTC-pinned session ✓; one-time non-idempotent TZ migration required on non-UTC hosts |
| `data/portfolio.json`                                                                                                                                                                        | JSON                | operational  | `ib_sync.py`, `futu_sync.py`, `server.py`                        | 8.7 K                    | atomic-save + SHA-256 checksum ✓                                                                     |
| `data/futu_portfolio.json`                                                                                                                                                                   | JSON                | operational  | `futu_sync.py`                                                   | 16 K                     | atomic-save ✓                                                                                        |
| `data/orders.json`                                                                                                                                                                           | JSON                | operational  | `ib_orders.py`                                                   | 1.1 K                    | snapshot of live orders                                                                              |
| `data/trade_log.json`                                                                                                                                                                        | JSON                | audit-log    | **ib_execute, ib_reconcile, ib_sync, fill_monitor, exit_orders** | 313 B                    | **5 writers, plain `open("w") + json.dump`** — P0                                                    |
| `data/nav_history.jsonl`                                                                                                                                                                     | JSONL               | audit-log    | `ib_sync.py`, `portfolio_performance.py` (likely read-only)      | 1.0 K                    | append-safe format ✓                                                                                 |
| `data/uw_analyze_cache.json`                                                                                                                                                                 | JSON                | cache        | `uw_analyze_cache.py`                                            | 5.5 M                    | asyncio-lock + tmpfile-replace ✓; LRU 300                                                            |
| `data/uw_analyze_history/`                                                                                                                                                                   | JSON tree           | audit-log    | `uw_analyze_cache.py`                                            | **358 M / 37,114 files** | **no retention** — P1                                                                                |
| `data/uw_unusual_flow_log.json`                                                                                                                                                              | JSON                | audit-log    | `uw_analyze_flow_tracker.py`                                     | 70 B                     | atomic ✓; `purge(older_than_days=30)` exists but never called                                        |
| `data/trend_scan.duckdb`                                                                                                                                                                     | DuckDB              | cache        | `scanners/trend/storage.py` + API background task                | 2.0 M                    | no visible `_WRITE_LOCK`; concurrent write risk                                                      |
| `data/trend_scan.json`                                                                                                                                                                       | JSON                | cache        | `api/server.py`                                                  | 31 K                     | overwrite on every run                                                                               |
| `data/ta.duckdb`                                                                                                                                                                             | DuckDB              | cache        | GitHub Action only                                               | 84 M                     | read-only at runtime ✓                                                                               |
| `data/cri_scheduled/`                                                                                                                                                                        | JSON snapshots      | audit-log    | `scanners/repair_cri_rvol_cache.py`, `scanners/trend/cli.py`     | 3.0 M / 380 files        | unbounded                                                                                            |
| `data/cri.json`, `gex.json`, `vcg.json`, `flow_analysis.json`, `discover.json`, `scanner.json`, `strategies.json`, `performance.json`, `ta_premarket_status.json`, `option_close_cache.json` | JSON                | cache        | scanners / reports                                               | various                  | overwrite on each run                                                                                |
| `data/presets/`                                                                                                                                                                              | JSON per-sector     | config+cache | `monitor_daemon/handlers/preset_rebalance.py`                    | 980 K / 152 files        | plain `open("w")` — concurrent call corrupts output                                                  |
| `data/uw_api_stats_history.json`                                                                                                                                                             | JSON                | audit-log    | `utils/uw_api_stats.py`                                          | 3.5 K                    | —                                                                                                    |
| `data/service_health/cta-sync.json`                                                                                                                                                          | JSON                | cache        | `services/cta_sync_service.py`                                   | 893 B                    | —                                                                                                    |
| `data/service_health/cta-sync-history.jsonl`                                                                                                                                                 | JSONL               | audit-log    | `services/cta_sync_service.py`                                   | 31 K                     | append-safe ✓                                                                                        |
| `data/company_info_cache/`, `price_history_cache/`, `seasonality_cache/`, `menthorq_cache/`, `analyst_ratings_cache.json`                                                                    | JSON                | cache        | fetchers                                                         | KB–MB                    | unbounded                                                                                            |
| `data/apex_mirror/`                                                                                                                                                                          | Parquet + JSON meta | cache        | `ta_lib/apex_sync.py`                                            | 450 M / 8,753 files      | atomic tmp→rename ✓; R2-outage fallback ✓                                                            |
| `data/universe/`                                                                                                                                                                             | JSON                | config       | manual/GitHub                                                    | 12 K                     | —                                                                                                    |
| `data/watchlist.json`                                                                                                                                                                        | JSON                | config       | UI/manual                                                        | 7.4 K                    | —                                                                                                    |
| `data/flex_token_config.json`                                                                                                                                                                | JSON                | config       | manual                                                           | 684 B                    | —                                                                                                    |
| `docs/status.md`                                                                                                                                                                             | Markdown            | audit-log    | evaluation pipeline                                              | variable                 | human-readable log mixed with code state; not machine-parseable                                      |

### 3.2 Problems

| File                                             | Problem                                                                                                             | Severity |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------- | -------- |
| `trade_log.json`                                 | 5 concurrent writers; plain `open + json.dump`; no lock, no atomic rename, no checksum                              | **P0**   |
| `uw_analyze_history/`                            | 37k files, 358 MB, no automatic janitor; cache code comments warn of this                                           | P1       |
| `cri_scheduled/`                                 | 380 timestamped snapshots, no rotation                                                                              | P2       |
| `data/presets/*.json`                            | Multi-writer via `preset_rebalance.py` without lock                                                                 | P2       |
| `orders.duckdb`                                  | Non-idempotent, manual `--apply` TZ migration on non-UTC hosts; failure mode is auto-`FAILED` of fresh PENDING rows | P1       |
| `trend_scan.duckdb`                              | No explicit lock between scanner process and API background task                                                    | P2       |
| `docs/status.md`                                 | Audit log as free-form markdown; not grep-stable, no schema                                                         | P2       |
| Per-ticker `asyncio.Lock` dict in UwAnalyzeCache | Not pruned when entries evict — the lock dict grows with unique tickers ever queried                                | P2       |
| Cross-cutting                                    | No shared migration framework. Every store carries its own one-shot script or none.                                 | P2       |

### 3.3 Target architecture (opinionated)

```
┌──────────────────────────────────────────────────────────────────────┐
│  OPERATIONAL (must survive restart, concurrent writers)              │
│   orders, positions, portfolio, reconciliation, WS tickets           │
│  → DuckDB with explicit per-table _WRITE_LOCK wrapper                │
│     (SQLite WAL would also work, DuckDB wins on analytical reads)    │
│     Single DB file: data/ops.duckdb, tables orders/positions/...     │
├──────────────────────────────────────────────────────────────────────┤
│  AUDIT / APPEND-ONLY (never truncate, long-retention)                │
│   trade_log, nav_history, uw_flow_log, orders_events,                │
│   uw_analyze_history, cri_scheduled                                  │
│  → JSONL only (line-buffered append-safe); one writer process        │
│    per file; rotate by month under data/audit/YYYY-MM/               │
│    Retention janitor: one script, cron nightly, age-based prune      │
├──────────────────────────────────────────────────────────────────────┤
│  SCANNER / ANALYTICAL CACHE (rebuildable)                            │
│   trend_scan, ta (R2 mirror), cri/gex/vcg/discover                   │
│  → DuckDB for trend_scan + ta (keep current shape)                   │
│     JSON for small per-scanner snapshots (keep current shape)        │
│     No change needed; it's the only layer that works well.           │
├──────────────────────────────────────────────────────────────────────┤
│  HOT CACHE / SESSION STATE                                           │
│   optionCloseCache, searchCache, company_info_cache, analyst cache   │
│  → in-process LRU with max-entries + max-age; no disk at all,        │
│    or cap-and-prune on each disk flush                               │
│    (avoid Redis until observed contention — adds ops surface)        │
├──────────────────────────────────────────────────────────────────────┤
│  CONFIG                                                              │
│   universe/, watchlist.json, flex_token_config.json, presets/        │
│  → keep JSON; freeze writers to UI/CLI or one scripted handler       │
└──────────────────────────────────────────────────────────────────────┘
```

Why not Postgres: single-writer FastAPI + scanner subprocesses don't justify a network-attached DB. DuckDB on local disk plus explicit locks matches current performance and avoids ops overhead.

Why not Redis: hot caches are small, no cross-process contention observed. Adds deployment surface without a paying win.

### 3.4 Migration phases (aligned with `feedback_zero_break_shim_refactors.md`)

**Phase P1 — "stop the bleed" (~1 PR, ~1 day).** Done when:

- [ ] `trade_log.json` writes funnel through a single `append_trade_log()` helper with `fcntl.flock` + atomic rename **or** become JSONL appends.
- [ ] `uw_analyze_history/` gets an age-based janitor; default 90-day retention.
- [ ] `optionCloseCache` gets max-entries + max-age prune on load and before persist.

**Phase P2 — "ops store consolidation" (~1 week).** Done when:

- [ ] New `data/ops.duckdb` created with `portfolio`, `futu_portfolio`, `orders` tables; old JSON files become JSONL snapshots of read paths.
- [ ] All writers re-pointed via a shim function (`save_portfolio(payload)`) so callers don't change.
- [ ] Old JSON files remain in place for one burn-in week; reads fallback.

**Phase P3 — "audit shape" (~3 days).** Done when:

- [ ] One unified `data/audit/{YYYY-MM}/{kind}.jsonl` layout.
- [ ] `docs/status.md` becomes a _render_ of the JSONL log, not the source.
- [ ] Shared `scripts/infra/audit_prune.py` covers all audit streams.

**Phase P4 — "cache discipline" (~3 days).** Done when:

- [ ] Every in-process cache declares `{max_entries, max_age, on_prune}`.
- [ ] Telemetry endpoint `/dev/cache-stats` returns counts for each cache (dev-only).
- [ ] Browser `_syncCache` is LRU-capped.

Each phase is independently revertible; no step requires rewriting callers before the next phase begins.

---

## 4. Memory audit

Verified every memory against current repo state on 2026-04-24.

| Memory                                        | Verdict               | One-line reason                                                                                                                                                                                                                                                                                                                |
| --------------------------------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `feedback_no_corporate_action_guessing.md`    | **keep**              | Still load-bearing; not code-coupled                                                                                                                                                                                                                                                                                           |
| `feedback_zero_break_shim_refactors.md`       | **keep**              | Phase P1–P4 plan in §3.4 explicitly relies on this preference                                                                                                                                                                                                                                                                  |
| `feedback_shim_vs_real_patching.md`           | **update**            | Shim paths referenced (`scripts.uw_scan`, `scripts.scanners.X`) no longer exist; current tests all patch real `xenon.*` paths. Rewrite the "How to apply" to: _verify patch target is `xenon.<bucket>.<module>`, which is now the only live path._ Keep the Why (binding vs import semantics) — that lesson is Python-general. |
| `feedback_live_e2e_surfaces_contract_bugs.md` | **keep**              | Still load-bearing and section 1 shows the pattern is alive in new tests                                                                                                                                                                                                                                                       |
| `project_pr_cd_handover.md`                   | **update**            | PR #29 merged 2026-04-21 (gh confirms). Strip pre-merge checklist, collapse "loose ends" to a bullet list and relocate to `docs/plans/2026-04-23-loose-ends.md` which the later memory references. Keep the "options tick-grid stub" + "rehydrate pool-lock design" items because they persist in code.                        |
| `project_pr_cd_ui_test_deferred.md`           | **delete**            | PR-C/D is merged; the UI QA happened. Zero future conversations need this memory.                                                                                                                                                                                                                                              |
| `project_scripts_reorg_phase1_shipped.md`     | **delete or rewrite** | **STALE.** `scripts/scanners/`, `scripts/fetchers/`, `scripts/execution/`, etc. do **not** exist. Current `scripts/` has only `infra/ lib/ migrations/ services/ ta_lib/ tests/`. The Phase 2 move already happened (those modules are now at `src/xenon/…`). If kept, rewrite entirely; otherwise delete.                     |
| `project_wizard_kickoff_2026-04-23.md`        | **keep**              | Active project state; burn-in waiver is a real policy decision future sessions need                                                                                                                                                                                                                                            |

### 4.1 Memories that should exist but don't

Draft new memories (each 2–4 lines), ready to save:

**`feedback_trade_log_write_discipline.md`** (feedback)

> Writes to `data/trade_log.json` (and sibling audit files) must use a single entry point with `fcntl.flock` + atomic rename, or switch to JSONL appends. Plain `open("w") + json.dump()` from multiple writers corrupts the log.
> **Why:** five writers (`ib_execute`, `ib_reconcile`, `ib_sync`, `fill_monitor`, `exit_orders`) currently share the file with no coordination; concurrent fills will tear the JSON.
> **How to apply:** any time an audit write is added, use a helper — do not call `json.dump()` directly; if you add a new writer, verify the helper is used.

**`feedback_market_hours_holiday_gap.md`** (feedback)

> The "closed-market gate" at `uw_analyze_cache.py:650-668` relies on `market_hours.is_market_open()`, which is weekend-only and documented as "simplified". A holiday calendar exists (`xenon.utils.market_calendar`) but is not consulted.
> **Why:** Every US market holiday passes the gate → auto-refreshes fire → ~1k UW calls/holiday × 9-10 holidays/yr leak against the 20k/day budget.
> **How to apply:** Any budget-gated code path must use a market-hours helper that consults `market_calendar`, not `market_hours.is_market_open` directly.

**`feedback_mock_xenonapi_antipattern.md`** (feedback)

> Web route tests that `vi.mock("@/lib/xenonApi")` and assert `mockXenonFetch.toHaveBeenCalled()` prove the mock fired, not the wire. Reject this pattern in review.
> **Why:** Section 1 of 2026-04-24 review catalogued 15+ such tests; the Bearer-header, endpoint, and method assertions are not covered anywhere. This is the same failure mode as `feedback_live_e2e_surfaces_contract_bugs.md`.
> **How to apply:** If the test's goal is route correctness, stub the underlying FastAPI response via `msw`/fetch-mock and let xenonFetch run for real. If the test's goal is component behavior, avoid the route.

**`reference_data_store_map.md`** (reference)

> Canonical inventory of Xenon's persistent stores: `docs/review/2026-04-24-codebase-review.md` §3.1. Single source of truth for "what lives where" — consult before adding a new cache/log/state file.
> **Why:** 30+ paths under `data/`, different write semantics, easy to misfile a new one.

**`reference_naked_short_table.md`** (reference) — optional

> Full guard decision matrix lives at `src/xenon/CLAUDE.md` (Naked Short Protection table). Cite when checking Gate 4 enforcement. Implementations: `web/lib/nakedShortGuard.ts`, `src/xenon/execution/naked_short_audit.py`.

### 4.2 Memories to merge

`project_pr_cd_handover.md` + `project_pr_cd_ui_test_deferred.md` overlap entirely on the same PR cycle. After update/delete per §4 table, only the updated handover remains.

---

## 5. CLAUDE.md review — gating bad behavior

### 5.1 Four Gates — enforceable from reading alone?

- **Gate 1 (convexity)** — prose only; no code pointer. A new assistant cannot verify. _Add a pointer:_ the `r:r` check site. (I couldn't locate a dedicated guard; looks like it's assumed in the structure-classification layer.)
- **Gate 2 (edge)** — prose only; this gate is analytical, not codeable. Leave as-is.
- **Gate 3 (Kelly 2.5% cap)** — prose only; no code pointer. **Add a pointer** to the place route's Kelly-cap enforcement (or note that it's currently not tested end-to-end — §1.1).
- **Gate 4 (no naked shorts)** — well-pointed: `web/lib/nakedShortGuard.ts`, `src/xenon/execution/naked_short_audit.py`, tests cited. ✓ The table under `src/xenon/CLAUDE.md` is exemplary.

### 5.2 Contradictions / ambiguity

| Issue                                                           | Location A                                                                  | Location B                              | Fix                                                                                      |
| --------------------------------------------------------------- | --------------------------------------------------------------------------- | --------------------------------------- | ---------------------------------------------------------------------------------------- |
| Memory "shims still in place" vs. repo truth                    | `project_scripts_reorg_phase1_shipped.md`                                   | `/bin/ls scripts/`                      | Memory is wrong, not CLAUDE.md. Memory §4 covers this.                                   |
| Scripts reorg doc drift                                         | root `CLAUDE.md` "# Scanner Hierarchy" says `src/xenon/scanners/_shared/` ✓ | memory says `scripts/scanners/_shared/` | CLAUDE.md is correct; memory stale.                                                      |
| "Never use Yahoo Finance" is in root CLAUDE.md but not enforced | root `CLAUDE.md` line 28                                                    | —                                       | Add a **pre-commit hook** that `grep`s diff for `yfinance\|yahoo` in staged Python files |
| `xenonFetch` is "never `spawn()`" policy                        | `src/xenon/api/CLAUDE.md` § Core rule                                       | `web/app/api/*`                         | Add a pre-commit hook that rejects `child_process.spawn\|spawnSync` under `web/app/api`  |

### 5.3 Rules that should be hooks, not prose

| Hook trigger                                      | Command                                                                                           | Blocks?           | Rationale                                                |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------- | ----------------- | -------------------------------------------------------- |
| `PreToolUse` on Write/Edit under `web/app/api/**` | `grep -nE "spawn(Sync)?\\(" <path> && exit 2`                                                     | Yes               | Root rule "Never `spawn()`" — can't rely on prose.       |
| Pre-commit staged diff                            | `git diff --cached -U0 -- '*.py' \| grep -E 'yfinance\|yahoo_fin\|yahoo-finance'`                 | Yes               | "Never Yahoo Finance" rule.                              |
| Pre-commit staged diff                            | `git diff --cached -U0 -- 'web/**/*.ts*' \| grep -E 'Math.abs\\(.*(credit\|price\|mid)'`          | Warn              | Preserves the sign-convention rule from `web/CLAUDE.md`. |
| Pre-commit staged diff                            | `git diff --cached --name-only \| xargs grep -l 'open(.*trade_log.json.*w)' 2>/dev/null`          | Yes               | `trade_log.json` plain-write guard (the §3 P0).          |
| Pre-commit staged diff                            | `git diff --cached --name-only -- 'src/xenon/**/*.py' \| xargs -r python3 -m ruff check --no-fix` | Yes if any error  | No ruff-in-CI → at least gate diff.                      |
| Pre-commit staged diff                            | `cd web && npx tsc --noEmit --incremental`                                                        | Yes if new errors | Stop 153-error drift from becoming 200.                  |
| `SessionStart` hook                               | Output `git log --oneline -n 10` plus `/bin/ls data/trade_log.json` size                          | No                | Context, not a gate.                                     |
| `PreToolUse` on Bash                              | Block `fnck` patterns: `--no-verify`, `--no-gpg-sign`                                             | Yes               | Matches global `CLAUDE.md` "never skip hooks" clause.    |

(Exact installation: use the `update-config` skill.)

### 5.4 Missing guardrails

- **"Every new long-lived cache must declare `max_entries` + `max_age`"** — add to `src/xenon/CLAUDE.md` § perf. Without it, §2 findings will recur.
- **"No `vi.mock("@/lib/xenonApi")` in route tests"** — add to `web/CLAUDE.md` § testing. Route tests should mock `fetch`/FastAPI response, not xenonApi itself.
- **"Every new `data/*.json` file must document: writer count, atomic-save, retention"** — add to `src/xenon/CLAUDE.md` alongside the data-files catalog reference.
- **"Any new external-API client must flow through a budget-gate helper"** — add to root `CLAUDE.md` next to the 20k/day budget note. Current wording describes _why_ but not _how_ the gate is enforced.
- **"Any cross-process contract (subprocess stdout/exit-code, HTTP ↔ route) must ship with at least one live test"** — encode the lesson from `feedback_live_e2e_surfaces_contract_bugs.md`.

### 5.5 Bloat

| Section                                                                           | Verdict                                                                                                                                                                                                                                       |
| --------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Root `CLAUDE.md` § Identity ("Xenon — market structure reconstruction…")          | **Keep** — short, referenced elsewhere                                                                                                                                                                                                        |
| Root `CLAUDE.md` § Startup Checklist                                              | **Keep** — actively used                                                                                                                                                                                                                      |
| Root `CLAUDE.md` § Tests (commands)                                               | **Keep** — used on every test session                                                                                                                                                                                                         |
| `src/xenon/CLAUDE.md` § Commands table (26 rows)                                  | **Trim** — most commands are discoverable via `--help`; keep the unusual ones (`trend-scan`, `uw-scan`, `evaluate`, `portfolio`, `sync`, `futu-sync`, `apex-refresh`) + remove the MenthorQ and Forex lines that duplicate `docs/reference/*` |
| `src/xenon/api/CLAUDE.md` § DuckDB timestamp migration (50 lines)                 | **Move** — this is a one-time op note, not policy. Relocate to `docs/runbooks/2026-04-21-orders-tz-migration.md` and keep a one-line pointer in CLAUDE.md                                                                                     |
| `src/xenon/api/CLAUDE.md` § Cancel/Modify failure propagation (7 numbered points) | **Keep, but split** — the operational points (1–4) are rules; point 7 is implementation detail about `applied_sequence`, move to inline code doc                                                                                              |
| `web/CLAUDE.md` § Calculations-Correctness Rules (the big code block)             | **Keep** — high-leverage, regression-preventing, actively referenced                                                                                                                                                                          |
| `web/CLAUDE.md` § uw-analyze cache-first loading (3 commit SHAs)                  | **Trim** — commit SHAs will rot; replace with a pointer to the hook file                                                                                                                                                                      |
| `brand/CLAUDE.md`                                                                 | **Keep as-is** — short, rule-dense                                                                                                                                                                                                            |

---

## Appendix — commands I ran (and would run)

```bash
# Ran during evidence gathering
git log --oneline --since="6 weeks ago" | head -80
gh pr view 29 --json state,mergedAt,title
/bin/ls /Users/chenxi/projects/xenon/data
/bin/ls /Users/chenxi/projects/xenon/scripts          # verified reorg memory stale
ruff check . --statistics                             # via subagent
grep -rn ": any" web/ --include="*.ts*" | wc -l       # via subagent
find web/components web/app -name "*.tsx" | xargs wc -l | sort -rn | head -20
grep -rn "monkeypatch.setattr" scripts/tests/ src/xenon/api/tests/ | head -80
python3.13 -c "import duckdb; c=duckdb.connect('data/orders.duckdb', read_only=True); print(c.execute('show tables').fetchall())"

# Would run next
python3.13 -m pytest scripts/tests/ src/xenon/api/tests/ \
    --cov=src/xenon --cov-report=term-missing --cov-report=html:/tmp/cov
cd web && npx vitest run --coverage
cd web && npx tsc --noEmit 2>&1 | tee /tmp/tsc-errors.log
py-spy record -o /tmp/portfolio_perf.svg -- \
    python3.13 -m xenon.reports.portfolio_performance --out /tmp/report.html
du -sh data/uw_analyze_history data/apex_mirror data/cri_scheduled data/presets
find data/uw_analyze_history -type f | wc -l
```

---

_End of review. 2026-04-24, against commit `add2f3f6` on `feat/position-order-quote-token`._
