# Test Suite Enhancement — Design Spec

**Date:** 2026-04-13
**Status:** Approved
**Goal:** Systematic test coverage infrastructure + prioritized gap fill to reach spec'd 95% coverage target

## Problem Statement

The project has 3,718 passing tests (2,126 Python, 1,592 Vitest) and 78 E2E specs, but:

- **Coverage drift:** New features shipped faster than tests. 103 web/lib source files have zero corresponding tests. Several recently spec'd features (uw-stats hooks, /trend-scan route, /uw-stats endpoints) shipped with no tests.
- **No detection mechanism:** No coverage thresholds, no CI gates, no tool to flag untested source files. Coverage can regress silently.
- **Duplicated test boilerplate:** IBClient mocks, Clerk auth mocks, fetch mocks, WebSocket mocks copy-pasted across dozens of files. A single API shape change cascaded into 65+ test failures.
- **E2E blind spots:** All 78 specs mock HTTP/WS — no real backend integration. Missing specs for trend-scan page, SSE streaming, order error paths.

## Non-Goals

- Full Istanbul instrumentation for E2E (too heavy for mock-based E2E suite)
- CI/CD pipeline setup (no pipeline exists; enforcement is local-only for now)
- Refactoring source code to be more testable (test what exists)
- Reaching 95% immediately (start at 80% floor, ratchet up)

---

## Phase 1 — Infrastructure (Guardrails)

### 1a. Test Gap Detector

**File:** `scripts/test_gap_detector.py`
**Dependencies:** None (stdlib only)

A script that reports source files without corresponding tests. Two scan modes:

**Python scan:**

- Source: `scripts/` (`.py` files, excluding `__init__.py`, `conftest.py`, test files, `__pycache__`)
- Test dir: `scripts/tests/`
- Match rule: `foo.py` → `test_foo.py`, with subdirectory awareness (`api/routes/uw_analyze.py` → `test_uw_analyze_route*.py`)
- Import-based indirect coverage: AST-parse test files to find imports, mark transitively-tested source files

**TypeScript scan:**

- Source: `web/lib/`, `web/app/api/` (`.ts`/`.tsx`, excluding type-only files `*.d.ts`, barrel `index.ts` with only re-exports)
- Test dir: `web/tests/`
- Match rule: `foo.ts` → `foo.test.ts` or `foo.test.tsx`

**Output:**

- Markdown table to stdout: `| Source File | Has Test? | Test File | Notes |`
- Summary line: `X/Y files covered (Z%)`
- Per-directory breakdown
- `--json` flag for machine consumption
- Exit code: 0 if orphan count <= threshold (default 50, configurable via `--max-orphans`), 1 otherwise

### 1b. Coverage Thresholds

**Python (`pyproject.toml` + `.coveragerc`):**

```toml
# pyproject.toml additions
[tool.pytest.ini_options]
addopts = "--cov=scripts --cov-config=.coveragerc --cov-fail-under=80"
```

```ini
# .coveragerc
[run]
source = scripts
omit =
    scripts/tests/*
    scripts/*/__pycache__/*
    scripts/data/*

[report]
fail_under = 80
exclude_lines =
    pragma: no cover
    if __name__ == .__main__.
    if TYPE_CHECKING:
    raise NotImplementedError
```

**TypeScript (`vitest.config.ts` additions):**

```ts
coverage: {
  // ... existing include/exclude ...
  thresholds: {
    lines: 80,
    functions: 75,
    branches: 70,
  },
},
```

Starting at 80/75/70 — ratchet up as Phase 2 fills gaps.

### 1c. E2E Route Manifest

**File:** `web/e2e/route-manifest.ts`

```ts
export const ROUTE_MANIFEST: Record<
  string,
  { specs: string[]; status: "covered" | "partial" | "missing" }
> = {
  "/": { specs: ["regime-dashboard.spec.ts"], status: "covered" },
  "/uw-analyze": {
    specs: ["uw-analyze.spec.ts", "uw-analyze-closed-market.spec.ts"],
    status: "partial",
  },
  "/trend-scan": { specs: [], status: "missing" },
  // ... all app routes
};
```

**Check script:** `scripts/check_e2e_coverage.ts` (or integrated into `test_gap_detector.py`)

- Compares manifest against actual `web/app/` route directories
- Reports: routes with `missing` status, specs referencing removed routes
- Warns on new routes not in manifest

### 1d. Shared Test Infrastructure

**Python — `scripts/tests/conftest.py` additions:**

```python
@pytest.fixture
def mock_ib_client():
    """Pre-configured IB client mock with common methods."""
    client = MagicMock()
    client.reqMktData = MagicMock()
    client.placeOrder = MagicMock()
    client.reqPositions = MagicMock()
    client.isConnected = MagicMock(return_value=True)
    return client

@pytest.fixture
def mock_uw_client():
    """UW API client mock with configurable response fixtures."""
    client = MagicMock()
    # Default: return empty but valid responses
    client.get_flow = AsyncMock(return_value=[])
    client.get_chain = AsyncMock(return_value={})
    return client

@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """Temp directory patched into all data/ path references."""
    monkeypatch.setenv("XENON_DATA_DIR", str(tmp_path))
    return tmp_path

@pytest.fixture
def frozen_market_time():
    """Context manager to freeze market hours checks to a fixed ET time."""
    @contextmanager
    def _freeze(hour, minute, weekday=0):
        fake_now = datetime(2026, 4, 13, hour, minute, tzinfo=ZoneInfo("America/New_York"))
        # weekday 0=Monday
        with patch('scripts.utils.market_hours.now_et', return_value=fake_now):
            yield fake_now
    return _freeze
```

**TypeScript — `web/tests/helpers/` directory:**

| File                    | Purpose                                                                       |
| ----------------------- | ----------------------------------------------------------------------------- |
| `mockFetch.ts`          | Configurable global fetch mock — success, error, timeout, SSE stream modes    |
| `mockWebSocket.ts`      | Reusable MockWebSocket class extracted from inline patterns in 10+ test files |
| `mockClerk.ts`          | Centralized Clerk auth mock (`@clerk/nextjs/server` + `@clerk/nextjs`)        |
| `fixtures/portfolio.ts` | Typed portfolio response fixtures                                             |
| `fixtures/uwAnalyze.ts` | Typed uw-analyze response fixtures (snapshot, portfolio, action items)        |
| `fixtures/orders.ts`    | Typed order response fixtures (open, executed, rejected)                      |
| `fixtures/regime.ts`    | Typed regime/CRI response fixtures                                            |

**E2E — `web/e2e/fixtures/` directory:**

| File         | Purpose                                                                                     |
| ------------ | ------------------------------------------------------------------------------------------- |
| `mockApi.ts` | Shared `page.route()` helpers for common API mocking. Individual specs import and override. |
| `mockWs.ts`  | Shared MockWebSocket `page.addInitScript()` helper                                          |
| `data/`      | JSON fixture files for common API responses                                                 |

---

## Phase 2 — Gap Fill (Priority Tiers)

### Tier 1 — Money Path (Order Handling, Portfolio)

| Source File                                 | Test File (new)                  | Test Type | Cases                                                                       |
| ------------------------------------------- | -------------------------------- | --------- | --------------------------------------------------------------------------- |
| `web/lib/order/orderModify.ts`              | `order-modify.test.ts`           | Vitest    | Modify payload construction, field validation, partial modify, combo modify |
| `web/lib/order/modifyOrderQuote.ts`         | `modify-order-quote.test.ts`     | Vitest    | Quote request shape, error propagation, stale quote handling                |
| `web/lib/order/openOrderCombos.ts`          | `open-order-combos.test.ts`      | Vitest    | Combo leg grouping, orphan leg handling, multi-expiry combos                |
| `web/lib/portfolio/portfolioByStructure.ts` | `portfolio-by-structure.test.ts` | Vitest    | Structure grouping, unknown structure fallback, empty portfolio             |
| `web/lib/portfolio/positionUtils.ts`        | `position-utils.test.ts`         | Vitest    | Position aggregation, multi-leg P&L math, sign conventions                  |

### Tier 2 — Recently Shipped Features (Spec'd at 95%, Under-Tested)

| Source File                                                 | Test File (new)                    | Test Type | Cases                                                                           |
| ----------------------------------------------------------- | ---------------------------------- | --------- | ------------------------------------------------------------------------------- |
| `web/lib/useUwStats.ts`                                     | `use-uw-stats.test.ts`             | Vitest    | Polling lifecycle, stale data, error states, unmount cleanup                    |
| `web/lib/useUwStatsHistory.ts`                              | `use-uw-stats-history.test.ts`     | Vitest    | History fetch, empty state, date range filtering                                |
| FastAPI `/uw-stats`, `/uw-stats/history`, `/uw-stats/reset` | `test_uw_stats_routes.py`          | pytest    | GET/POST endpoints, auth, validation, reset confirmation                        |
| FastAPI `POST /trend-scan`                                  | `test_trend_scan_route.py`         | pytest    | Subprocess spawn, 180s timeout, scheduler trigger, error codes, pre-market gate |
| uw-analyze SSE streaming                                    | `uw-analyze-sse.test.ts`           | Vitest    | Stream parsing, reconnection on drop, partial frame, multi-ticker ordering      |
| uw-analyze cross-service                                    | `test_uw_analyze_orchestration.py` | pytest    | Cache → diff → flow tracker → action items full pipeline                        |
| uw-analyze sticky fields                                    | `test_uw_analyze_sticky.py`        | pytest    | Sticky field merge under 429s, stale field diff false negatives                 |
| uw-analyze semaphore                                        | `test_uw_analyze_concurrency.py`   | pytest    | 4th request when 3 slots full, timeout behavior, queue fairness                 |

### Tier 3 — Real-Time Data Plumbing

| Source File                    | Test File (new)              | Test Type | Cases                                                       |
| ------------------------------ | ---------------------------- | --------- | ----------------------------------------------------------- |
| `web/lib/reconnectStrategy.ts` | `reconnect-strategy.test.ts` | Vitest    | Backoff math, max retries, jitter bounds, reset on success  |
| `web/lib/quoteTelemetry.ts`    | `quote-telemetry.test.ts`    | Vitest    | Event shape, batching, flush on unmount                     |
| `web/lib/criCalc.ts`           | `cri-calc.test.ts`           | Vitest    | CRI scoring math, edge inputs (NaN, zero, negative)         |
| `web/lib/criCache.ts`          | `cri-cache.test.ts`          | Vitest    | Cache TTL, staleness flag, concurrent reads                 |
| `web/lib/usePrices.ts`         | `use-prices.test.ts`         | Vitest    | WS subscription lifecycle, price merge, unmount unsubscribe |

### Tier 4 — E2E Flow Coverage

| Spec File (new)              | Route         | Cases                                                                             |
| ---------------------------- | ------------- | --------------------------------------------------------------------------------- |
| `trend-scan.spec.ts`         | `/trend-scan` | Page load, sort columns, expand row, score bar rendering, empty state             |
| `uw-analyze-sse.spec.ts`     | `/uw-analyze` | SSE stream arrival, incremental tile rendering, refresh button triggers re-stream |
| `order-error.spec.ts`        | Order entry   | Rejection display, modify failure, timeout fallback, combo rejection              |
| `uw-analyze-refresh.spec.ts` | `/uw-analyze` | Manual refresh flow, user_initiated flag, closed-market override                  |

---

## Estimated Scope

| Phase     | Deliverables        | New Files                          | Estimated Test Cases    |
| --------- | ------------------- | ---------------------------------- | ----------------------- |
| 1a        | Gap detector script | 1                                  | —                       |
| 1b        | Coverage thresholds | 2 modified + 1 new                 | —                       |
| 1c        | E2E route manifest  | 1-2                                | —                       |
| 1d        | Shared test infra   | 8-10 (helpers, fixtures, conftest) | —                       |
| 2 Tier 1  | Money path tests    | 5                                  | ~25-30                  |
| 2 Tier 2  | Recent features     | 8                                  | ~50-60                  |
| 2 Tier 3  | Real-time plumbing  | 5                                  | ~30-35                  |
| 2 Tier 4  | E2E specs           | 4                                  | ~20-25                  |
| **Total** |                     | **~35-40 new files**               | **~125-150 test cases** |

## Success Criteria

1. `scripts/test_gap_detector.py` runs clean — all Tier 1-3 source files have corresponding tests
2. `pytest` passes with `--cov-fail-under=80`
3. `npm test` passes with vitest coverage thresholds (80/75/70)
4. E2E route manifest shows zero `missing` routes for shipped features
5. All 3,718+ existing tests still green (zero regressions)
6. Shared helpers used by new tests — no new copy-pasted mocks

## Build Order

Phase 1 (infrastructure) ships first as a standalone commit. Phase 2 tiers ship sequentially — each tier is independently valuable and testable. Tier 1 before Tier 2 because money path bugs are highest cost.
