# Memory Usage And Performance Report

Date: 2026-04-22

## Scope

This report focuses on likely memory-retention and runtime-cost hotspots in the current Xenon codebase, based on repo inspection rather than live heap captures. The goal is to identify practical ways to reduce steady-state RSS, lower browser tab memory, and cut avoidable CPU/network churn.

## Context Reviewed

- `src/xenon/api/services/uw_analyze_cache.py`
- `scripts/infra/ib_realtime/ib_realtime_server.js`
- `web/lib/useSyncHook.ts`
- `web/lib/usePrices.ts`
- `web/lib/useChainPrefetch.ts`
- `src/xenon/reports/evaluate.py`
- `src/xenon/reports/portfolio_performance.py`
- `web/CLAUDE.md`
- recent commits via `git log --oneline -n 12`

## Executive Summary

The codebase already contains some deliberate memory controls, but the remaining risk is concentrated in three areas:

1. Long-lived process caches that still grow with usage history
2. Browser hooks that keep cross-mount snapshots or prefetched data resident
3. Python report/evaluation paths that materialize large intermediate structures at once

The strongest near-term opportunities are:

- cap or prune the remaining unbounded caches
- reduce duplicated in-memory payloads for analysis/reporting paths
- add explicit memory budgets and instrumentation so regressions become visible before they become incidents

## Evidence And Findings

### 1. `uw_analyze_cache` already documents a real memory incident

`src/xenon/api/services/uw_analyze_cache.py` explicitly notes that RSS had surged past 7 GB before entry caps were added. That means memory pressure here is not hypothetical.

What is already good:

- `_MAX_ENTRIES` bounds the live cache
- `previous` snapshots are collapsed to light derived payloads
- `materialized_changes` is capped
- orphan per-ticker locks are swept during eviction

Remaining risk:

- archive history is intentionally unbounded
- each live entry still stores a full `current` snapshot
- cache persistence rewrites the full JSON file, which creates transient allocation pressure during large writes

### 2. IB realtime server still has at least one clearly unbounded persistent cache

In `scripts/infra/ib_realtime/ib_realtime_server.js`:

- `fundamentalsStore` is LRU-capped at 500
- `searchCache` is capped at 200 with TTL
- `optionCloseCache` is an uncapped `Map` persisted to `data/option_close_cache.json`

`optionCloseCache` is the clearest steady-state memory growth candidate on the Node side. Every distinct option contract ever seen can remain in memory and on disk indefinitely.

Secondary observation:

- `symbolStates`, `symbolSubscribers`, `clientSymbols`, `snapshotRequests`, and `requestIdToSymbol` appear to have lifecycle cleanup paths, so they are less suspicious than `optionCloseCache`

### 3. Browser sync cache is process-global and unbounded by policy

`web/lib/useSyncHook.ts` uses a module-level `_syncCache` that survives unmount/remount for the browser session. That is a sensible UX optimization, but there is no TTL, size bound, or eviction policy.

Current risk is moderate rather than critical because most endpoints are fixed and few in number. The risk increases if more endpoint keys become parameterized or if payload size grows.

### 4. Option-chain prefetch trades responsiveness for per-tab memory

`web/lib/useChainPrefetch.ts` keeps a `Map<expiry, strikes[]>` for all prefetched expirations for the current ticker. This is not a leak by itself because it resets on ticker change, but it does intentionally retain all prefetched strike arrays for the life of the ticker-detail session.

This is a reasonable trade-off today. It becomes expensive if:

- more expirations are prefetched
- strike payloads grow
- multiple ticker-detail surfaces mount in parallel

### 5. `usePrices` is controlled, but it can accumulate large state objects under broad subscriptions

`web/lib/usePrices.ts` maintains `prices` and `fundamentals` as growing objects keyed by symbol/contract. Subscription diffing is good, and websocket lifecycle handling looks disciplined. The main memory risk is not a leak pattern but broad subscription sets causing large reactive state objects and frequent rerenders.

This is a UI memory plus CPU issue rather than a server memory issue.

### 6. Python evaluation/reporting paths still materialize large working sets

Two places stand out:

- `src/xenon/reports/evaluate.py`
- `src/xenon/reports/portfolio_performance.py`

`evaluate.py` runs multiple milestones in parallel and keeps all results resident in a `raw` dict before gating later stages. For one ticker this is fine. For many tickers, `run_evaluations()` wisely avoids exploding concurrency, but it still processes each ticker with fairly chunky milestone payloads.

`portfolio_performance.py` is the heavier memory candidate:

- pandas DataFrames are used repeatedly
- full marks histories are loaded into `marks_by_contract`
- parallel fallback fetches can populate many full history dicts at once
- several summary structures coexist before final payload emission

This is likely the biggest Python-side candidate for peak memory reduction.

## Approaches

### Approach A: Fast Memory Caps

Apply hard bounds to remaining session-lifetime caches and add retention cleanup.

Examples:

- add max-entry or age-based pruning to `optionCloseCache`
- add TTL plus max-size eviction to `_syncCache`
- add retention pruning for `uw_analyze_history`

Pros:

- fastest path to lower steady-state RSS
- low design risk
- easy to validate with focused tests

Cons:

- does not reduce peak memory inside heavy report jobs
- may trade a small amount of latency for cache misses

### Approach B: Reduce Payload Shape And Duplication

Trim what gets stored and passed around, especially in Python reporting and browser state.

Examples:

- store only fields actually rendered by the UI in client-side caches
- compress or normalize analysis snapshots before persistence
- in `portfolio_performance.py`, stream or chunk histories instead of building all full structures simultaneously
- replace some pandas-heavy intermediate steps with lighter dict/list math where vectorization is not materially helping

Pros:

- best path to reducing both peak memory and serialization overhead
- often improves CPU and response time too

Cons:

- higher implementation complexity
- higher regression risk than simple cache caps

### Approach C: Instrumentation And Budgets First

Add memory observability before major refactors.

Examples:

- log cache sizes and approximate bytes for `uw_analyze_cache`, `optionCloseCache`, and websocket symbol state
- expose lightweight debug counters for browser/session caches
- add a memory benchmark or smoke script for `portfolio_performance.py`
- define RSS or object-count budgets in tests for the highest-risk surfaces

Pros:

- reduces guesswork
- makes later optimization work measurable
- prevents regressions after fixes land

Cons:

- does not improve memory by itself
- can become busywork if not paired with concrete caps/refactors

## Recommendation

Use a combined plan:

1. Approach A first for immediate containment
2. Approach C in parallel so future work is measurable
3. Approach B next for the heaviest Python and browser payload paths

This order gives the best risk-adjusted return. The repo already shows that memory incidents can happen in long-lived services. Immediate containment should come before deeper rewrites.

## Ranked Improvement Ideas

### Priority 1: Cap `optionCloseCache`

File:

- `scripts/infra/ib_realtime/ib_realtime_server.js`

Recommendation:

- convert `optionCloseCache` to an LRU cache or add max-age plus max-entries pruning
- persist only recent contracts
- prune on load and before persist

Why:

- this is the clearest uncapped Node-side cache
- it is persisted, so growth survives restarts

Expected impact:

- lower steady-state Node RSS
- smaller disk cache file
- lower JSON stringify/parse overhead during persist/load

### Priority 2: Add retention to `uw_analyze_history`

File:

- `src/xenon/api/services/uw_analyze_cache.py`

Recommendation:

- add a janitor by file count, age, or total bytes per ticker
- keep enough history for debugging, not infinite archives

Why:

- the file itself documents retention as “none in v1”
- disk growth eventually feeds back into scan cost and operational drag

Expected impact:

- mostly disk and I/O, but also lower incidental memory during history reads and maintenance tasks

### Priority 3: Bound browser `_syncCache`

File:

- `web/lib/useSyncHook.ts`

Recommendation:

- replace the raw `Map` with a tiny LRU plus TTL
- consider storing only `data` fields needed for instant paint, not the full response body, for especially large endpoints

Why:

- current design is session-lifetime with no eviction policy
- safe today, but brittle as more endpoints or larger payloads are added

Expected impact:

- lower browser tab memory over long sessions
- fewer stale large payloads hanging around after route churn

### Priority 4: Audit wide websocket subscriptions and state fanout

Files:

- `web/lib/usePrices.ts`
- `scripts/infra/ib_realtime/ib_realtime_server.js`

Recommendation:

- measure typical subscription cardinality
- split hot quote state from colder fundamentals state where possible
- avoid pushing large state object updates for symbols not currently visible

Why:

- object growth and rerender pressure can dominate perceived performance even without a leak

Expected impact:

- smoother UI under broad watchlists or option-chain usage
- lower browser CPU and memory churn

### Priority 5: Reduce peak memory in `portfolio_performance.py`

File:

- `src/xenon/reports/portfolio_performance.py`

Recommendation:

- profile peak memory during `build_payload()`
- identify DataFrames that can be replaced with lighter structures
- fetch/process histories in batches instead of holding all contract histories in memory at once
- consider emitting the final response incrementally where possible

Why:

- this path combines pandas, parallel fetch, and full-history aggregation
- it is the best candidate for lowering peak Python RSS

Expected impact:

- lower report-generation memory spikes
- possibly faster response time for large portfolios

### Priority 6: Trim milestone payloads in `evaluate.py`

File:

- `src/xenon/reports/evaluate.py`

Recommendation:

- keep raw milestone payloads only until the decision gate that needs them
- downsample or summarize intermediate results after each gate
- avoid carrying large arrays into the final result unless they are user-visible

Why:

- this is more about disciplined payload lifecycle than a leak

Expected impact:

- modest memory improvement
- cleaner evaluation result structure

## Concrete Design Suggestions

### Suggestion 1: Standardize cache policy across the repo

Introduce a shared policy vocabulary:

- `max_entries`
- `max_age_ms`
- `max_bytes` where practical
- `prune_on_write`
- `prune_on_load`

That would reduce the current mix of bespoke `Map`, `OrderedDict`, and ad hoc retention logic.

### Suggestion 2: Add lightweight memory telemetry

Examples:

- Node: log `process.memoryUsage()` plus cache sizes every N minutes in development
- Python: log cache entry counts and approximate serialized bytes after cache persist
- Browser: optional dev-only panel for `_syncCache` entry count and active price subscription count

This should be dev-focused and cheap, not a production-grade telemetry project.

### Suggestion 3: Define memory budgets

Examples:

- `optionCloseCache` max entries
- `uw_analyze_cache` max live entries and max archive retention
- browser sync cache max endpoints
- portfolio performance max peak RSS target in benchmark mode

Budgets make future reviews easier because “acceptable growth” becomes explicit.

## Suggested Phased Plan

### Phase 1: Containment

- cap `optionCloseCache`
- add `uw_analyze_history` retention
- add TTL/LRU to `_syncCache`

### Phase 2: Measurement

- add cache-size counters and memory logs
- create one reproducible benchmark for `portfolio_performance.py`
- capture baseline numbers before larger refactors

### Phase 3: Structural Reduction

- trim `portfolio_performance.py` intermediates
- reduce evaluation payload lifetime in `evaluate.py`
- narrow websocket/browser state to visible data where practical

## Expected Outcome

If the work is executed in that order, the most likely gains are:

- lower long-session memory growth in Node and browser runtimes
- smaller operational footprint for long-lived FastAPI and realtime processes
- reduced peak memory during portfolio/performance jobs
- fewer performance regressions because caches and retention become explicit instead of incidental

## Final Recommendation

The highest-value move is not a broad rewrite. It is a targeted memory-governance pass:

- bound every long-lived cache
- prune archives intentionally
- measure peak memory in the heavy Python report path

That should improve performance materially with far less risk than trying to “optimize everything” at once.
