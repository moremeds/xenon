# Progress Report

## Session: 2026-03-17 — Performance Page Speed Optimization

### Summary
Implemented a comprehensive speed optimization for the `/performance` page, reducing cold-path load time from 30-90s to 10-20s and warm-path from 30-90s to 2-5s.

### Changes Made

#### T1: Price History Cache (`scripts/utils/price_cache.py`)
- New disk cache module with SHA-256 filenames in `data/price_history_cache/{stocks,options}/`
- TTL: 15 min during market hours, 24h after close
- Atomic writes via `atomic_save()`, thread-safe pruning (max 500 files)

#### T2: Cache Tests (`scripts/tests/test_price_cache.py`)
- 17 tests: cache keys, SHA-256 filenames, read/write, TTL expiry, corruption recovery, market hours, pruning

#### T3: Parallel Fetches (`scripts/portfolio_performance.py`)
- Refactored `build_payload()` into two-phase architecture:
  - Phase A: IB stock history (main thread, sequential) with cache check
  - Phase B: `ThreadPoolExecutor` for UW/Yahoo fallbacks + all option fetches
- Per-worker UWClient instances (thread-safe)
- `PERF_FETCH_WORKERS` env var (default 8, clamped 1-20)
- Estimated: 14 sequential @ 2s = 28s -> ceil(14/8) x 2s ~ 4s

#### T4: Parallel Fetch Tests (`scripts/tests/test_portfolio_performance.py`)
- 33 tests total (8 existing + 25 new): worker count edge cases, IB-only fetch, UW/Yahoo fallback, rate limit handling, cache hits, parallel orchestration

#### T5: FastAPI Background Endpoint (`scripts/api/server.py`)
- `POST /performance/background` — fire-and-forget, 202 response, task registry dedup
- `POST /performance` — piggybacks on in-flight task (no duplicate builds)
- `_write_cache()` now atomic (temp file + `os.replace()`)

#### T5b: Dedup Tests (`scripts/tests/test_performance_lock.py`)
- 6 tests: POST returns result, background 202, dedup already_running, concurrent POST piggyback, atomic write, no metadata leak

#### T6: SWR Route Migration (`web/app/api/performance/route.ts`)
- Warm path: return stale cache immediately + trigger background rebuild (5s fire-and-forget)
- Cold start: block on synchronous POST /performance (180s timeout)
- All freshness gates preserved

#### T7: Route Tests (`web/tests/performance-route.test.ts`)
- 9 tests: fresh cache, SWR stale + background, cold-start block, 502 on cold failure, background failure swallowed, freshness gates

### Test Results
- **56 Python tests** — all passing
- **9 TypeScript tests** — all passing
- **Total new tests: 52** (17 + 6 + 25 + 4)

### Expected Performance Impact
| Scenario | Before | After |
|----------|--------|-------|
| Cold start (no cache) | 30-90s | 10-20s |
| Warm cache (within TTL) | 30-90s | 2-5s |
| Concurrent requests (warm) | Both block 190s | First rebuilds, rest get cached |
| Market-hours refresh | 30-90s blocking | Instant + background rebuild |

### Files Changed
- `scripts/utils/price_cache.py` (new)
- `scripts/tests/test_price_cache.py` (new)
- `scripts/tests/test_performance_lock.py` (new)
- `scripts/portfolio_performance.py` (refactored)
- `scripts/tests/test_portfolio_performance.py` (updated)
- `scripts/api/server.py` (updated)
- `web/app/api/performance/route.ts` (updated)
- `web/tests/performance-route.test.ts` (updated)
- `CLAUDE.md` (updated docs)

---

## Session: 2026-03-09

### Changes Made

#### 1. Next.js 15 Upgrade & UI Architecture Overhaul
**Files**: `web/package.json`, `web/next.config.mjs`, `web/components/WorkspaceSections.tsx`, `web/components/ui/Skeleton.tsx`, `web/lib/store.ts`

- Upgraded Next.js to v15.3.2 (with Turbopack) and React to v19.0.0.
- Replaced the slugggish `POST /api/prices` proxy with a direct client-to-server WebSocket snapshot connection in `usePrices.ts`, drastically improving initial TTFB and layout rendering.
- De-coupled the monolithic global React Contexts (`OrderActionsContext`, `TickerDetailContext`) by implementing a lightweight `Zustand` store for UI state management. This eliminates full-page re-renders when deep UI state (like opening a ticker modal) changes.
- Implemented a standard UI `Skeleton` and `TableSkeleton` for loading states on tables (Blotter, Journal, Orders), preventing jarring layout shifts during initial fetches or IB background syncs.

#### 2. E2E Browser Automation shifted from Puppeteer to Playwright
**Files**: `web/package.json`, `web/e2e/prices-performance.test.js`, Memory

- Enforced rule to exclusively use Playwright for browser automation.
- Re-wrote performance evaluation tests to use Playwright's `chromium` engine instead of Puppeteer.
- E2E tests verified the newly implemented direct WebSocket connection resolves data and renders the WorkspaceShell without sluggishness.

#### 3. Bug Fix Workflow — TDD + E2E mandatory
**Files**: `CLAUDE.md`, `.pi/AGENTS.md`, `~/.claude/CLAUDE.md`

Established permanent rule: all bug fixes require red/green TDD (failing test first, then fix, then green). UI bugs additionally require a Playwright E2E test for confirmation. Updated all three instruction files.

#### 2. VIX/VVIX live badge + timestamp on /regime
**Files**: `web/components/RegimePanel.tsx`, `web/app/globals.css`, `web/e2e/regime-vix-live-badge.spec.ts`

- Added `vixLastTs` / `vvixLastTs` state tracking last live WS tick timestamp (updated via `useEffect` on `liveVix`/`liveVvix`)
- New `.regime-strip-ts` element in VIX and VVIX strip cells: shows `HH:MM:SS` when live, `---` when no data
- Added `data-testid="strip-vix"` and `data-testid="strip-vvix"` for reliable E2E targeting
- 6 Playwright E2E tests (badge presence, timestamp element, `---` default) — all green

#### 3. Portfolio auto-sync — IB as source of truth
**Files**: `web/app/api/portfolio/route.ts`, `web/tests/portfolio-auto-sync.test.ts`

`GET /api/portfolio` now triggers background `ib_sync.py` when `portfolio.json` mtime >60s. Stale-while-revalidate: always serves immediately, syncs in background. Concurrency-guarded with module-level flag. 4 tests.

#### 4. Realized P&L — date filter bug fix
**Files**: `web/lib/realized-pnl.ts`, `web/tests/realized-pnl-date-filter.test.ts`

**Root cause**: `ib_insync.fills()` is session-scoped across calendar days. `orders.json` held prior-day fills (e.g. ILF closure at -$6,835) and `computeRealizedPnlFromFills()` summed all of them without date filtering.

Fix: added `todayET()` + `fillDateET()` (ET-aware, DST-correct) and filter fills to today before summing. 6 new tests including DST boundary and UTC/ET midnight edge cases.

#### 5. Day Move — mid price fallback
**Files**: `web/lib/dayMoveBreakdown.ts` (NEW), `web/components/MetricCards.tsx`

Extracted `computeDayMoveBreakdown` from MetricCards into a testable lib module. Added `resolveLastOrMid()`: uses `last` if positive, falls back to `(bid+ask)/2` for illiquid options/pre-market. Positions with no `last` but valid bid/ask are now included; breakdown shows `(MID)` suffix for transparency. 15 tests.

#### 6. IB delayed tick types — VIX/VVIX null data fix
**Files**: `scripts/ib_tick_handler.js` (NEW), `scripts/ib_realtime_server.js`, `web/tests/ib-delayed-ticks.test.ts`

**Root cause**: `reqMarketDataType(4)` (Delayed-Frozen) causes IB to send delayed tick types (66-76) for instruments without a real-time subscription (VIX, VVIX require separate CBOE index subscription). The original switch only handled live tick types (1-14) — delayed ticks hit `default: break` and all fields stayed null forever.

Fix: extracted tick handling into `scripts/ib_tick_handler.js` (pure, testable). Added all 8 delayed tick cases: `DELAYED_BID(66)`, `DELAYED_ASK(67)`, `DELAYED_LAST(68)`, `DELAYED_HIGH(72)`, `DELAYED_LOW(73)`, `DELAYED_VOLUME(74)`, `DELAYED_CLOSE(75)`, `DELAYED_OPEN(76)`. `updateDerivedLast()` promotes `close -> last` so VIX/VVIX populate automatically from DELAYED_CLOSE. `ib_realtime_server.js` imports from handler — no logic duplication. 16 tests.

### Verified
- All new tests green: 6 (VIX badge E2E) + 4 (portfolio sync) + 6 (realized PnL) + 15 (day move mid) + 16 (delayed ticks) = 47 tests
- `tsc --noEmit` — no new type errors
- VIX/VVIX: confirmed delayed tick type IDs via runtime (`DELAYED_CLOSE=75`, `DELAYED_OPEN=76`, `DELAYED_VOLUME=74`)
- Restart `ib_realtime_server.js` to activate delayed tick fix

---

## Session: 2026-03-07

### Changes Made

#### 1. MenthorQ CTA Positioning Data Integration
**Files**: `scripts/fetch_menthorq_cta.py` (NEW), `scripts/cri_scan.py` (MODIFIED), `scripts/tests/test_menthorq_cta.py` (NEW)

Integrated institutional CTA positioning data from MenthorQ into the CRI Scanner (Strategy 6). MenthorQ renders CTA tables as images — the script screenshots them via headless Playwright, extracts structured data via Claude Haiku Vision, and caches daily.

**New script** (`fetch_menthorq_cta.py`):
- Headless Chromium via Playwright logs into MenthorQ
- Screenshots 4 CTA table cards (main, index, commodity, currency)
- Claude Haiku Vision extracts structured JSON per table
- Daily cache at `data/menthorq_cache/cta_{DATE}.json`
- `resolve_trading_date()` handles weekends/pre-market
- CLI: `--json`, `--date`, `--force`, `--no-headless`

**CRI Scanner integration** (`cri_scan.py`):
- `run_analysis()` loads MenthorQ cache, adds `menthorq_cta` field to results
- Console summary shows SPX CTA position, percentile, z-score
- HTML report includes full MenthorQ CTA Positioning section:
  - SPX highlight cards (position, percentile, z-score)
  - Full tables for Main + Index assets (6 columns)
  - Compact tables for Commodity + Currency (4 columns)
  - Graceful "unavailable" fallback when no cache exists

**Tests** (`test_menthorq_cta.py`): 20 tests — cache read/write, find_by_underlying, vision parsing, trading date resolution, CRI integration shape.

#### 2. Credential security fix
**Files**: `scripts/fetch_menthorq_cta.py`, `.env` (NEW, gitignored), `scripts/tests/test_menthorq_cta.py`

Moved MenthorQ credentials from hardcoded defaults to project root `.env` file loaded via `python-dotenv`.
- Created `.env` at project root (gitignored) with `MENTHORQ_USER` and `MENTHORQ_PASS`
- Removed hardcoded credential defaults from `resolve_menthorq_creds()`
- Added `load_dotenv()` call at script import time
- Added 5 credential tests including source code inspection for leaked secrets
- Amended and force-pushed to erase credentials from git history

**Credentials architecture:**
- Root `.env` — Python scripts (MenthorQ creds) via `python-dotenv`
- `web/.env` — Next.js app (API keys) via built-in loading
- Web app reads cached MenthorQ data from `data/menthorq_cache/`, never fetches directly

#### 3. Documentation updates
**Files**: `CLAUDE.md`, `.pi/AGENTS.md`, `docs/strategies.md`, `README.md`

- Added credentials architecture table to CLAUDE.md
- Updated README setup section: two `.env` files, Playwright deps
- Added `menthorq-cta` command and `fetch_menthorq_cta.py` script to all command/script tables
- Added `data/menthorq_cache/` to data files references
- Added MenthorQ CTA Positioning section to Strategy 6 in `docs/strategies.md`

### Verified
- 25/25 new MenthorQ tests pass (including 5 credential security tests)
- 53/53 existing CRI tests pass (no regressions)
- Live fetch successful: 37 assets extracted across 4 tables from MenthorQ (2026-03-06 data)
- SPX CTA position: 0.45, 3M Percentile: 13, Z-Score: -1.56
- No credentials in source code (verified by `test_no_hardcoded_defaults`)

---

## Session: 2026-03-06

### Changes Made

#### 1. Fix combo order price resolution in ModifyOrderModal
**Files**: `web/components/ModifyOrderModal.tsx`, `web/components/WorkspaceSections.tsx`

Clicking "Modify" on a BAG (combo/spread) order previously showed "Market data unavailable for combo orders." The per-leg prices were available in the WebSocket feed but the modal couldn't look them up without portfolio leg details.

- Added `portfolio` prop to `ModifyOrderModal`
- Extended `resolveOrderPriceData()` to handle BAG orders: finds matching portfolio position, iterates legs via `legPriceKey()`, computes sign-weighted net BID/ASK/LAST, returns synthetic `PriceData`
- Removed the `isBag` early-exit rendering block — BAG orders now show BID/MID/ASK with working quick-set buttons
- Passed `portfolio` prop from `WorkspaceSections.tsx`

#### 2. Fix triplicate executed orders on /orders page
**Files**: `web/lib/OrderActionsContext.tsx`, `web/components/WorkspaceSections.tsx`

Cancelled orders appeared in triplicate in the Today's Executed Orders table (35 entries instead of 32).

**Root cause**: `setInterval` with async callbacks in cancel/modify polling. When IB sync took >5 seconds, overlapping interval ticks would all detect `!stillOpen` and each add a duplicate `cancelledOrder` entry to state.

- Replaced `setInterval` with chained `setTimeout` in both `startCancelPoll` and `startModifyPoll` — next tick only starts after previous async operation completes
- Updated cleanup to use `clearTimeout` instead of `clearInterval`
- Added `permId`-based deduplication in `allExecutedRows` memo as a safety net

#### 3. Per-leg P&L in combo position rows
**Files**: `web/components/PositionTable.tsx`

Expanded combo position rows (e.g., bull call spreads) now show per-leg P&L in the P&L column.

- Added P&L computation to `LegRow`: `sign x (|marketValue| - |entryCost|)`
- LONG legs show profit when option appreciates, SHORT legs show profit when option decays
- Red/green color coding matches position-level P&L styling
- Sum of per-leg P&L equals the position-level P&L (verified with AAOI spread)

#### 4. Documentation updates
**Files**: `CLAUDE.md`

- Added "Per-Leg P&L" calculation rules to Calculations section
- Added BAG modify modal price resolution to Price Resolution Priority table

### Verified
- `tsc --noEmit` — no new type errors
- Orders page: 32 entries (no triplicates), combo last prices resolved
- Portfolio page: AAOI expanded legs show -$16,170 / +$5,030 P&L, summing to position-level -$11,140
