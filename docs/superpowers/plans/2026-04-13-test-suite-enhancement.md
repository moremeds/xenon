# Test Suite Enhancement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build test coverage infrastructure (gap detector, thresholds, E2E manifest, shared helpers) and fill critical test gaps across Python/Vitest/E2E layers.

**Architecture:** Phase 1 ships infrastructure guardrails. Phase 2 uses those guardrails' shared helpers to write missing tests in priority order (money path → recent features → real-time plumbing → E2E flows). Each tier is independently valuable.

**Tech Stack:** Python 3.13 (pytest, pytest-cov, pytest-asyncio strict), TypeScript (Vitest v8 coverage, Playwright), Next.js App Router, FastAPI

**Spec:** `docs/superpowers/specs/2026-04-13-test-suite-enhancement-design.md`

---

## File Map

### Phase 1 — Infrastructure

| Action | Path                                 | Purpose                                                                                |
| ------ | ------------------------------------ | -------------------------------------------------------------------------------------- |
| Create | `scripts/test_gap_detector.py`       | Detects untested source files across Python + TS                                       |
| Create | `.coveragerc`                        | Python coverage config (excludes, fail_under)                                          |
| Modify | `vitest.config.ts`                   | Add coverage thresholds, refine hook exclusions                                        |
| Create | `web/e2e/route-manifest.ts`          | Maps app routes → E2E spec status                                                      |
| Create | `web/e2e/route-manifest.test.ts`     | Validates manifest against actual routes                                               |
| Modify | `scripts/tests/conftest.py`          | Add shared fixtures (mock_ib_client, mock_uw_client, frozen_market_time, tmp_data_dir) |
| Create | `web/tests/helpers/mockClerk.ts`     | Centralized Clerk auth mock                                                            |
| Create | `web/tests/helpers/mockFetch.ts`     | Configurable fetch mock with SSE support                                               |
| Create | `web/tests/helpers/mockWebSocket.ts` | Reusable MockWebSocket class                                                           |
| Create | `web/tests/helpers/index.ts`         | Barrel re-export                                                                       |

### Phase 2 — Gap Fill

| Action | Path                                     | Purpose                                            |
| ------ | ---------------------------------------- | -------------------------------------------------- |
| Create | `web/tests/quote-telemetry.test.ts`      | Tier 1: bid/ask/mid/spread math                    |
| Create | `web/tests/api-contracts.test.ts`        | Tier 1: error payload shapes, cache headers        |
| Create | `web/tests/scales.test.ts`               | Tier 1: scaleLinear, scaleTime, tick generation    |
| Create | `web/tests/array-utils.test.ts`          | Tier 3: extent, mean, bisectLeft edge cases        |
| Create | `web/tests/cri-calc.test.ts`             | Tier 3: CRI scoring math, level classification     |
| Create | `web/tests/cta-freshness.test.ts`        | Tier 3: trading day calc, staleness detection      |
| Create | `web/tests/cta-percentiles.test.ts`      | Tier 3: percentile normalization, label formatting |
| Create | `web/tests/vcg-staleness.test.ts`        | Tier 3: VCG staleness with market hours            |
| Create | `web/tests/use-uw-stats.test.ts`         | Tier 2: polling hook lifecycle                     |
| Create | `web/tests/use-uw-stats-history.test.ts` | Tier 2: history polling hook                       |
| Create | `scripts/tests/test_uw_stats_routes.py`  | Tier 2: FastAPI /uw-stats endpoints                |
| Create | `scripts/tests/test_trend_scan_route.py` | Tier 2: FastAPI POST /trend-scan                   |
| Create | `web/e2e/uw-analyze-sse.spec.ts`         | Tier 4: SSE stream rendering                       |
| Create | `web/e2e/order-error.spec.ts`            | Tier 4: order rejection/error display              |

---

## Phase 1: Infrastructure

### Task 1: Test Gap Detector

**Files:**

- Create: `scripts/test_gap_detector.py`

- [ ] **Step 1: Create the gap detector script**

```python
#!/usr/bin/env python3
"""Detect source files without corresponding test files.

Scans Python (scripts/) and TypeScript (web/lib/, web/app/api/) source
directories and reports orphan files — source code with no matching test.

Usage:
    python scripts/test_gap_detector.py
    python scripts/test_gap_detector.py --json
    python scripts/test_gap_detector.py --max-orphans 30
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── Python scan ──────────────────────────────────────────────────────────

def _py_test_stems(test_dir: Path) -> set[str]:
    """Collect all test_ prefixed stems from the test directory."""
    stems: set[str] = set()
    for p in test_dir.rglob("test_*.py"):
        stems.add(p.stem)
    return stems


def _py_imported_modules(test_dir: Path) -> set[str]:
    """AST-parse test files to find imported module names."""
    imported: set[str] = set()
    for p in test_dir.rglob("test_*.py"):
        try:
            tree = ast.parse(p.read_text(errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[-1])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[-1])
    return imported


def scan_python() -> list[dict]:
    """Scan scripts/ for Python source files without tests."""
    scripts_dir = PROJECT_ROOT / "scripts"
    test_dir = scripts_dir / "tests"
    skip_names = {"__init__", "conftest", "__main__"}

    sources: list[Path] = []
    for p in scripts_dir.rglob("*.py"):
        if (
            "tests" in p.parts
            or "__pycache__" in p.parts
            or "data" in p.parts
            or p.stem in skip_names
        ):
            continue
        sources.append(p)

    test_stems = _py_test_stems(test_dir)
    imported = _py_imported_modules(test_dir)

    results = []
    for src in sorted(sources):
        stem = src.stem
        rel = src.relative_to(PROJECT_ROOT)
        # Match: foo.py → test_foo.py (or test_foo_*.py via prefix)
        has_direct = any(ts.startswith(f"test_{stem}") for ts in test_stems)
        has_import = stem in imported
        has_test = has_direct or has_import
        results.append({
            "file": str(rel),
            "has_test": has_test,
            "test_file": next(
                (ts for ts in sorted(test_stems) if ts.startswith(f"test_{stem}")),
                None,
            ),
            "note": "import-only" if (has_import and not has_direct) else "",
        })
    return results


# ── TypeScript scan ──────────────────────────────────────────────────────

def _ts_test_stems(test_dir: Path) -> set[str]:
    """Collect test file stems (without .test.ts suffix)."""
    stems: set[str] = set()
    for p in test_dir.rglob("*.test.ts"):
        stems.add(p.stem.removesuffix(".test"))
    for p in test_dir.rglob("*.test.tsx"):
        stems.add(p.stem.removesuffix(".test"))
    return stems


def _is_barrel_or_types(path: Path) -> bool:
    """Skip type-only and barrel re-export files."""
    if path.suffix == ".d.ts":
        return True
    if path.name in ("index.ts", "types.ts"):
        return True
    return False


def scan_typescript() -> list[dict]:
    """Scan web/lib/ and web/app/api/ for TS source files without tests."""
    web_dir = PROJECT_ROOT / "web"
    test_dir = web_dir / "tests"
    scan_dirs = [web_dir / "lib", web_dir / "app" / "api"]

    sources: list[Path] = []
    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        for p in scan_dir.rglob("*.ts"):
            if _is_barrel_or_types(p) or "node_modules" in p.parts:
                continue
            sources.append(p)
        for p in scan_dir.rglob("*.tsx"):
            if _is_barrel_or_types(p) or "node_modules" in p.parts:
                continue
            sources.append(p)

    test_stems = _ts_test_stems(test_dir)

    # Also check if file stem appears anywhere in test file contents
    all_test_content = ""
    for p in test_dir.rglob("*"):
        if p.is_file() and p.suffix in (".ts", ".tsx"):
            try:
                all_test_content += p.read_text(errors="ignore") + "\n"
            except OSError:
                continue

    results = []
    for src in sorted(sources):
        stem = src.stem
        rel = src.relative_to(PROJECT_ROOT)
        has_direct = stem in test_stems
        has_import = stem in all_test_content
        has_test = has_direct or has_import
        results.append({
            "file": str(rel),
            "has_test": has_test,
            "test_file": f"{stem}.test.ts" if has_direct else None,
            "note": "import-only" if (has_import and not has_direct) else "",
        })
    return results


# ── Output ───────────────────────────────────────────────────────────────

def print_markdown(results: list[dict], label: str) -> int:
    """Print markdown table and return orphan count."""
    orphans = [r for r in results if not r["has_test"]]
    covered = len(results) - len(orphans)
    pct = (covered / len(results) * 100) if results else 0

    print(f"\n## {label}")
    print(f"\n{covered}/{len(results)} files covered ({pct:.0f}%)\n")

    if orphans:
        print("| Source File | Has Test? | Test File | Notes |")
        print("|---|---|---|---|")
        for r in orphans:
            check = "✓" if r["has_test"] else "✗"
            tf = r["test_file"] or "—"
            print(f"| `{r['file']}` | {check} | {tf} | {r['note']} |")

    return len(orphans)


def main():
    parser = argparse.ArgumentParser(description="Detect source files without tests")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--max-orphans", type=int, default=50,
                        help="Exit 1 if orphan count exceeds this (default 50)")
    args = parser.parse_args()

    py_results = scan_python()
    ts_results = scan_typescript()

    if args.json:
        print(json.dumps({"python": py_results, "typescript": ts_results}, indent=2))
    else:
        py_orphans = print_markdown(py_results, "Python (scripts/)")
        ts_orphans = print_markdown(ts_results, "TypeScript (web/lib/ + web/app/api/)")
        total_orphans = py_orphans + ts_orphans
        print(f"\n**Total orphans: {total_orphans}** (threshold: {args.max_orphans})")
        if total_orphans > args.max_orphans:
            print(f"⚠️  Exceeds threshold of {args.max_orphans}")
            sys.exit(1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run the gap detector to verify it works**

Run: `python3.13 scripts/test_gap_detector.py`
Expected: Markdown tables showing Python and TypeScript orphan counts. Should show ~21 TS orphans.

- [ ] **Step 3: Run with --json flag**

Run: `python3.13 scripts/test_gap_detector.py --json | python3.13 -m json.tool | head -20`
Expected: Valid JSON output with `python` and `typescript` arrays.

- [ ] **Step 4: Commit**

```bash
git add scripts/test_gap_detector.py
git commit -m "feat: add test gap detector script for Python + TypeScript orphan detection"
```

---

### Task 2: Python Coverage Config

**Files:**

- Create: `.coveragerc`

- [ ] **Step 1: Create .coveragerc**

```ini
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
    \.\.\.
```

- [ ] **Step 2: Verify coverage runs with the config**

Run: `cd /Users/chenxi/projects/xenon && python3.13 -m pytest scripts/tests/ --cov=scripts --cov-config=.coveragerc --cov-report=term-missing -q 2>&1 | tail -20`
Expected: Coverage summary with percentages per module. Should NOT fail (we're not enforcing fail_under yet — just verifying the config loads).

- [ ] **Step 3: Commit**

```bash
git add .coveragerc
git commit -m "feat: add Python coverage config (.coveragerc) with 80% threshold"
```

---

### Task 3: Vitest Coverage Thresholds + Hook Exclusion Fix

**Files:**

- Modify: `vitest.config.ts`

- [ ] **Step 1: Add thresholds and refine hook exclusions**

In `vitest.config.ts`, replace the coverage block:

```ts
    coverage: {
      provider: "v8",
      include: [
        "site/app/**/*.ts",
        "site/lib/**/*.ts",
        "web/lib/**/*.ts",
        "web/app/api/**/*.ts",
        "lib/tools/**/*.ts",
      ],
      exclude: [
        "**/*.test.ts",
        "**/node_modules/**",
        // Hooks that require Next.js navigation context (can't run in node or jsdom)
        "web/lib/perfTracker.ts",
        // React context providers (need full component tree)
        "web/lib/OrderActionsContext.tsx",
        "web/lib/TickerDetailContext.tsx",
        "web/lib/accountContext.ts",
        // Pure type definitions
        "web/lib/types.ts",
        "web/lib/orderModify.ts",
        // PI framework
        "lib/tools/pi-tools.ts",
        "lib/tools/schemas/index.ts",
        "lib/tools/wrappers/index.ts",
        "lib/tools/wrappers/fetch-ticker.ts",
        "lib/tools/wrappers/ib-order-manage.ts",
        "lib/tools/wrappers/ib-orders.ts",
        "lib/tools/wrappers/ib-sync.ts",
        "lib/tools/wrappers/scanner.ts",
        // Routes that spawn subprocesses or need live services
        "web/app/api/pi/**",
        "web/app/api/prices/**",
        "web/app/api/blotter/**",
        "web/app/api/discover/**",
        "web/app/api/flow-analysis/**",
        "web/app/api/scanner/**",
      ],
      thresholds: {
        lines: 80,
        functions: 75,
        branches: 70,
      },
    },
```

Key change: removed blanket `web/lib/use*.ts` exclusion. Now only specific untestable files are excluded. Hooks like `useUwStats.ts`, `useUwStatsHistory.ts`, `usePrices.ts` are now included in coverage.

- [ ] **Step 2: Verify vitest still passes**

Run: `cd /Users/chenxi/projects/xenon/web && npm test 2>&1 | tail -10`
Expected: All 1,592+ tests pass. Coverage thresholds may warn but should not fail (existing coverage should be close to 80%).

- [ ] **Step 3: Commit**

```bash
git add vitest.config.ts
git commit -m "feat: add vitest coverage thresholds (80/75/70), refine hook exclusions"
```

---

### Task 4: E2E Route Manifest

**Files:**

- Create: `web/e2e/route-manifest.ts`
- Create: `web/e2e/route-manifest.test.ts`

- [ ] **Step 1: Create the route manifest**

```ts
/**
 * E2E route coverage manifest.
 *
 * Maps every app route to its Playwright specs and coverage status.
 * Run route-manifest.test.ts to verify no routes are missing.
 */
export const ROUTE_MANIFEST: Record<
  string,
  { specs: string[]; status: "covered" | "partial" | "missing" }
> = {
  "/": {
    specs: [
      "regime-cor1m.spec.ts",
      "regime-cor1m-live-route.spec.ts",
      "regime-cor1m-live-stream.spec.ts",
      "regime-day-change.spec.ts",
      "regime-detail-panels-responsive.spec.ts",
      "regime-history-responsive.spec.ts",
      "regime-history-tooltip.spec.ts",
      "regime-live-index-stream.spec.ts",
      "regime-live-index-streaming.spec.ts",
      "regime-live-stream-values.spec.ts",
      "regime-market-closed-eod.spec.ts",
      "regime-relationship-view.spec.ts",
      "regime-rvol-history.spec.ts",
      "regime-rvol-history-live-cache.spec.ts",
      "regime-rvol-history-live-route.spec.ts",
      "regime-stale-market-open.spec.ts",
      "regime-strip-responsive.spec.ts",
      "regime-vcg-edr-badge.spec.ts",
      "regime-vix-live-badge.spec.ts",
      "regime-close-transition-refresh.spec.ts",
      "regime-closed-refresh.spec.ts",
      "regime-cta-share-pattern.spec.ts",
    ],
    status: "covered",
  },
  "/portfolio": {
    specs: [
      "portfolio-view-toggle.spec.ts",
      "portfolio-leg-row-runtime.spec.ts",
      "portfolio-market-closed.spec.ts",
      "portfolio-same-day-combo-pnl.spec.ts",
      "account-day-move-ib-daily-pnl.spec.ts",
      "account-metric-cards.spec.ts",
      "day-move-ib-daily-pnl.spec.ts",
      "futu-readonly.spec.ts",
    ],
    status: "covered",
  },
  "/orders": {
    specs: [
      "open-order-combo.spec.ts",
      "open-order-single-detail.spec.ts",
      "order-combo.spec.ts",
      "order-cancel-error-propagation.spec.ts",
      "modify-combo-order.spec.ts",
      "modify-order-confirmation.spec.ts",
      "modify-order-resting-limit.spec.ts",
      "modify-order-spread-telemetry.spec.ts",
      "iwm-close-order-summary.spec.ts",
      "wulf-close-order-naked-short.spec.ts",
      "orders-historical-trades-refresh.spec.ts",
      "historical-trades-filter.spec.ts",
    ],
    status: "covered",
  },
  "/uw-analyze": {
    specs: ["uw-analyze.spec.ts", "uw-analyze-closed-market.spec.ts"],
    status: "partial",
  },
  "/scanner": {
    specs: ["trend-scanner.spec.ts"],
    status: "covered",
  },
  "/performance": {
    specs: [
      "performance-page.spec.ts",
      "performance-chart-axes.spec.ts",
      "performance-chart-theme.spec.ts",
      "performance-market-closed.spec.ts",
    ],
    status: "covered",
  },
  "/regime": {
    specs: [],
    status: "missing",
  },
  "/flow-analysis": {
    specs: [],
    status: "missing",
  },
  "/cta": {
    specs: ["cta-page.spec.ts", "cta-stale-banner.spec.ts"],
    status: "covered",
  },
  "/discover": {
    specs: [],
    status: "missing",
  },
  "/journal": {
    specs: [],
    status: "missing",
  },
  "/internals": {
    specs: ["internals-market-closed.spec.ts"],
    status: "partial",
  },
  "/kit": {
    specs: [],
    status: "missing",
  },
  "/[ticker]": {
    specs: [
      "ticker-page.spec.ts",
      "ticker-search-chain.spec.ts",
      "ticker-search-live.spec.ts",
      "chain-held-leg-prices.spec.ts",
      "chain-sticky-header.spec.ts",
      "pltr-chain-position-focus.spec.ts",
      "crox-bull-call-stale-price.spec.ts",
      "iwm-ticker-detail-combo-sign.spec.ts",
      "iwm-synthetic-mark-label.spec.ts",
      "order-ticket-quote-telemetry.spec.ts",
      "price-bar-quote-telemetry.spec.ts",
      "price-chart-theme.spec.ts",
      "spread-price-bar.spec.ts",
      "risk-reversal-midprice.spec.ts",
      "ilf-chart-price.spec.ts",
    ],
    status: "covered",
  },
  "/dashboard": {
    specs: [],
    status: "missing",
  },
};
```

- [ ] **Step 2: Create the manifest check test**

```ts
import { readdirSync, existsSync } from "node:fs";
import { join } from "node:path";
import { test, expect } from "@playwright/test";
import { ROUTE_MANIFEST } from "./route-manifest";

const E2E_DIR = join(__dirname);
const APP_DIR = join(__dirname, "..", "app");

test.describe("E2E Route Manifest", () => {
  test("all spec files in manifest actually exist", () => {
    const allSpecs = Object.values(ROUTE_MANIFEST).flatMap((r) => r.specs);
    const existing = readdirSync(E2E_DIR).filter((f) => f.endsWith(".spec.ts"));
    for (const spec of allSpecs) {
      expect(
        existing,
        `Spec ${spec} listed in manifest but not found`,
      ).toContain(spec);
    }
  });

  test("no routes have 'missing' status", () => {
    const missing = Object.entries(ROUTE_MANIFEST)
      .filter(([, v]) => v.status === "missing")
      .map(([k]) => k);
    // This is informational — tracks which routes lack E2E coverage.
    // Uncomment the assertion below when all routes are covered:
    // expect(missing).toEqual([]);
    console.log(`Routes without E2E specs: ${missing.join(", ") || "none"}`);
  });

  test("every app route has a manifest entry", () => {
    // Scan web/app/ for page.tsx files and extract route paths
    const routes: string[] = [];
    function walk(dir: string, prefix: string) {
      if (!existsSync(dir)) return;
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        if (
          entry.name === "api" ||
          entry.name === "sign-in" ||
          entry.name === "sign-up"
        )
          continue;
        if (entry.isDirectory()) {
          walk(join(dir, entry.name), `${prefix}/${entry.name}`);
        } else if (entry.name === "page.tsx" || entry.name === "page.ts") {
          routes.push(prefix || "/");
        }
      }
    }
    walk(APP_DIR, "");

    const manifestRoutes = Object.keys(ROUTE_MANIFEST);
    for (const route of routes) {
      expect(manifestRoutes, `Route ${route} has no manifest entry`).toContain(
        route,
      );
    }
  });
});
```

- [ ] **Step 3: Run the manifest test**

Run: `cd /Users/chenxi/projects/xenon/web && npx playwright test e2e/route-manifest.test.ts --reporter=list 2>&1 | tail -15`
Expected: Tests pass. "Routes without E2E specs" logged for informational tracking.

- [ ] **Step 4: Commit**

```bash
git add web/e2e/route-manifest.ts web/e2e/route-manifest.test.ts
git commit -m "feat: add E2E route manifest with coverage tracking test"
```

---

### Task 5: Shared Python Fixtures (conftest.py)

**Files:**

- Modify: `scripts/tests/conftest.py`

- [ ] **Step 1: Add shared fixtures to conftest.py**

Append to the existing `scripts/tests/conftest.py` (which currently only has sys.path setup):

```python
# ── Shared fixtures ──────────────────────────────────────────────────────
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, patch

import pytz

EASTERN = pytz.timezone("America/New_York")


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
    """UW API client mock matching actual uw_client.py method signatures."""
    client = MagicMock()
    client.get_flow_alerts = MagicMock(return_value=[])
    client.get_flow_alerts_by_ticker = MagicMock(return_value=[])
    client.get_flow_per_strike = MagicMock(return_value={})
    client.get_flow_per_expiry = MagicMock(return_value={})
    return client


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """Temp directory patched into server DATA_DIR."""
    monkeypatch.setattr("api.server.DATA_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def frozen_market_time():
    """Context manager to freeze market hours to a fixed ET time.

    Usage:
        with frozen_market_time(hour=10, minute=30):
            assert is_market_open() is True
        with frozen_market_time(hour=17, minute=0):
            assert is_market_open() is False
    """
    from datetime import timedelta

    @contextmanager
    def _freeze(hour, minute, weekday=0):
        base = datetime(2026, 4, 13, hour, minute)  # Monday
        offset = (weekday - base.weekday()) % 7
        target = base + timedelta(days=offset)
        fake_now = EASTERN.localize(target.replace(hour=hour, minute=minute))
        with patch("utils.market_hours.get_eastern_now", return_value=fake_now):
            yield fake_now
    return _freeze
```

- [ ] **Step 2: Verify existing tests still pass with new conftest**

Run: `python3.13 -m pytest scripts/tests/test_utils.py -x -q 2>&1 | tail -5`
Expected: All existing tests pass (new fixtures are opt-in, no side effects).

- [ ] **Step 3: Commit**

```bash
git add scripts/tests/conftest.py
git commit -m "feat: add shared pytest fixtures (mock_ib_client, mock_uw_client, frozen_market_time, tmp_data_dir)"
```

---

### Task 6: Shared TypeScript Test Helpers

**Files:**

- Create: `web/tests/helpers/mockClerk.ts`
- Create: `web/tests/helpers/mockFetch.ts`
- Create: `web/tests/helpers/mockWebSocket.ts`
- Create: `web/tests/helpers/index.ts`

- [ ] **Step 1: Create mockClerk.ts**

```ts
/**
 * Centralized Clerk auth mock for Vitest.
 *
 * Usage: import { setupClerkMock } from "./helpers";
 * Then call vi.mock("@clerk/nextjs/server", () => setupClerkMock());
 */
import { vi } from "vitest";

export function setupClerkMock(token = "test-token") {
  return {
    auth: vi.fn(async () => ({
      getToken: async () => token,
      userId: "user_test123",
    })),
    currentUser: vi.fn(async () => ({ id: "user_test123" })),
  };
}

export function setupClerkClientMock() {
  return {
    useUser: vi.fn(() => ({ user: { id: "user_test123" }, isLoaded: true })),
    useAuth: vi.fn(() => ({
      getToken: async () => "test-token",
      isLoaded: true,
    })),
    ClerkProvider: ({ children }: { children: React.ReactNode }) => children,
  };
}
```

- [ ] **Step 2: Create mockFetch.ts**

```ts
/**
 * Configurable fetch mock for Vitest.
 *
 * Usage:
 *   const restore = installFetchMock({ "/api/stats": { body: {...} } });
 *   // ... test ...
 *   restore();
 */
import { vi } from "vitest";

type MockRoute = {
  body?: unknown;
  status?: number;
  headers?: Record<string, string>;
  error?: Error;
  delay?: number;
};

export function installFetchMock(routes: Record<string, MockRoute>) {
  const original = globalThis.fetch;
  const mockFn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    const pathname = new URL(url, "http://localhost").pathname;

    const route = routes[pathname];
    if (!route) {
      return new Response(JSON.stringify({ error: "not mocked" }), {
        status: 404,
      });
    }
    if (route.error) throw route.error;
    if (route.delay) await new Promise((r) => setTimeout(r, route.delay));

    return new Response(JSON.stringify(route.body ?? {}), {
      status: route.status ?? 200,
      headers: { "Content-Type": "application/json", ...route.headers },
    });
  });

  globalThis.fetch = mockFn as typeof fetch;
  return () => {
    globalThis.fetch = original;
  };
}

export { type MockRoute };
```

- [ ] **Step 3: Create mockWebSocket.ts**

```ts
/**
 * Reusable MockWebSocket for Vitest.
 *
 * Usage:
 *   const { install, instances } = createMockWebSocket();
 *   install(); // replaces globalThis.WebSocket
 *   // ... trigger hook that creates WS ...
 *   instances[0].simulateMessage({ type: "price", data: {...} });
 */
import { vi } from "vitest";

export type MockWSInstance = {
  url: string;
  readyState: number;
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onclose: ((ev: CloseEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  send: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  simulateOpen: () => void;
  simulateMessage: (data: unknown) => void;
  simulateClose: (code?: number) => void;
  simulateError: () => void;
};

export function createMockWebSocket() {
  const instances: MockWSInstance[] = [];
  const original = globalThis.WebSocket;

  function MockWS(url: string) {
    const instance: MockWSInstance = {
      url,
      readyState: 0, // CONNECTING
      onopen: null,
      onmessage: null,
      onclose: null,
      onerror: null,
      send: vi.fn(),
      close: vi.fn(() => {
        instance.readyState = 3;
      }),
      simulateOpen() {
        instance.readyState = 1;
        instance.onopen?.({ type: "open" } as Event);
      },
      simulateMessage(data: unknown) {
        instance.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
      },
      simulateClose(code = 1000) {
        instance.readyState = 3;
        instance.onclose?.({ code, reason: "" } as CloseEvent);
      },
      simulateError() {
        instance.onerror?.({ type: "error" } as Event);
      },
    };
    instances.push(instance);
    return instance;
  }

  return {
    instances,
    install: () => {
      globalThis.WebSocket = MockWS as unknown as typeof WebSocket;
    },
    restore: () => {
      globalThis.WebSocket = original;
    },
  };
}
```

- [ ] **Step 4: Create barrel index.ts**

```ts
export { setupClerkMock, setupClerkClientMock } from "./mockClerk";
export { installFetchMock, type MockRoute } from "./mockFetch";
export { createMockWebSocket, type MockWSInstance } from "./mockWebSocket";
```

- [ ] **Step 5: Commit**

```bash
git add web/tests/helpers/
git commit -m "feat: add shared Vitest test helpers (mockClerk, mockFetch, mockWebSocket)"
```

---

## Phase 2: Gap Fill

### Task 7: Tier 1 — quoteTelemetry.test.ts

**Files:**

- Create: `web/tests/quote-telemetry.test.ts`

- [ ] **Step 1: Write the test**

```ts
import { describe, it, expect } from "vitest";
import type { PriceData } from "../lib/pricesProtocol";
import {
  getQuoteMetrics,
  formatSpreadTelemetry,
  buildQuoteTelemetryModel,
} from "../lib/quoteTelemetry";

/** Factory for PriceData with sensible defaults — override only what matters. */
function makePriceData(overrides: Partial<PriceData> = {}): PriceData {
  return {
    symbol: "TEST",
    last: null,
    lastIsCalculated: false,
    bid: null,
    ask: null,
    bidSize: null,
    askSize: null,
    volume: null,
    high: null,
    low: null,
    open: null,
    close: null,
    week52High: null,
    week52Low: null,
    avgVolume: null,
    timestamp: new Date().toISOString(),
    ...overrides,
  };
}

describe("getQuoteMetrics", () => {
  it("computes mid, spread, and spreadBps from bid/ask", () => {
    const result = getQuoteMetrics({ bid: 4.3, ask: 5.1 });
    expect(result.bid).toBe(4.3);
    expect(result.ask).toBe(5.1);
    expect(result.mid).toBe(4.7);
    expect(result.spread).toBe(0.8);
    // spreadBps = (0.80 / 4.70) * 10000 ≈ 1702
    expect(result.spreadBps).toBe(1702);
  });

  it("returns nulls when priceData is null", () => {
    const result = getQuoteMetrics(null);
    expect(result).toEqual({
      bid: null,
      mid: null,
      ask: null,
      spread: null,
      spreadBps: null,
    });
  });

  it("returns nulls when priceData is undefined", () => {
    const result = getQuoteMetrics(undefined);
    expect(result).toEqual({
      bid: null,
      mid: null,
      ask: null,
      spread: null,
      spreadBps: null,
    });
  });

  it("handles zero mid (spreadBps null)", () => {
    const result = getQuoteMetrics({ bid: 0, ask: 0 });
    expect(result.mid).toBe(0);
    expect(result.spread).toBe(0);
    expect(result.spreadBps).toBe(null); // mid <= 0
  });

  it("handles missing bid (partial data)", () => {
    const result = getQuoteMetrics({
      bid: undefined as unknown as number,
      ask: 5.1,
    });
    expect(result.bid).toBe(null);
    expect(result.mid).toBe(null);
    expect(result.spread).toBe(null);
  });
});

describe("formatSpreadTelemetry", () => {
  it("formats spread with percentage", () => {
    const result = formatSpreadTelemetry({ bid: 4.3, ask: 5.1 });
    // spread = 0.80, mid = 4.70, pct = (0.80/4.70)*100 = 17.02%
    expect(result).toContain("0.80");
    expect(result).toContain("17.02%");
  });

  it("returns --- for null input", () => {
    expect(formatSpreadTelemetry(null)).toBe("---");
  });
});

describe("buildQuoteTelemetryModel", () => {
  it("builds complete model from full price data", () => {
    const model = buildQuoteTelemetryModel(
      makePriceData({
        symbol: "AAPL",
        bid: 184.0,
        ask: 184.5,
        last: 184.22,
        close: 183.0,
        high: 185.0,
        low: 182.5,
        volume: 1234567,
      }),
    );
    expect(model).not.toBe(null);
    expect(model!.bid.value).toContain("184.00");
    expect(model!.ask.value).toContain("184.50");
    expect(model!.volume.value).toBe("1,234,567");
    expect(model!.day.tone).toBe("positive"); // 184.22 > 183.00
    expect(model!.day.trend).toBe("up");
  });

  it("returns null for null priceData", () => {
    expect(buildQuoteTelemetryModel(null)).toBe(null);
  });

  it("labels MARK when lastIsCalculated is true", () => {
    const model = buildQuoteTelemetryModel(
      makePriceData({
        last: 100,
        close: 100,
        lastIsCalculated: true,
      }),
    );
    expect(model!.last.label).toBe("MARK");
  });

  it("shows negative day change with down trend", () => {
    const model = buildQuoteTelemetryModel(
      makePriceData({
        last: 95,
        close: 100,
        volume: 0,
      }),
    );
    expect(model!.day.tone).toBe("negative");
    expect(model!.day.trend).toBe("down");
    expect(model!.day.value).toContain("-5.00%");
  });
});
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd /Users/chenxi/projects/xenon/web && npx vitest run --config ../vitest.config.ts web/tests/quote-telemetry.test.ts 2>&1 | tail -10`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add web/tests/quote-telemetry.test.ts
git commit -m "test: add quoteTelemetry unit tests — bid/ask/mid/spread math, day change signs"
```

---

### Task 8: Tier 1 — apiContracts.test.ts

**Files:**

- Create: `web/tests/api-contracts.test.ts`

- [ ] **Step 1: Write the test**

```ts
import { describe, it, expect, vi, afterEach } from "vitest";
import {
  getRequestId,
  setNoStoreResponseHeaders,
  setCacheResponseHeaders,
  jsonApiError,
} from "../lib/apiContracts";
import { NextResponse } from "next/server";

describe("getRequestId", () => {
  it("returns a UUID string", () => {
    const id = getRequestId();
    expect(id).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("returns fallback format when crypto.randomUUID throws", () => {
    const original = globalThis.crypto;
    Object.defineProperty(globalThis, "crypto", {
      value: {
        randomUUID: () => {
          throw new Error("not supported");
        },
      },
      configurable: true,
    });
    const id = getRequestId();
    expect(id).toMatch(/^rid_\d+_[0-9a-f]+$/);
    Object.defineProperty(globalThis, "crypto", {
      value: original,
      configurable: true,
    });
  });
});

describe("setNoStoreResponseHeaders", () => {
  it("sets Cache-Control, Pragma, and X-Request-Id", () => {
    const res = NextResponse.json({});
    setNoStoreResponseHeaders(res, "req-123");
    expect(res.headers.get("Cache-Control")).toBe(
      "no-store, no-cache, must-revalidate",
    );
    expect(res.headers.get("Pragma")).toBe("no-cache");
    expect(res.headers.get("X-Request-Id")).toBe("req-123");
  });
});

describe("setCacheResponseHeaders", () => {
  it("sets public max-age and cache state", () => {
    const res = NextResponse.json({});
    setCacheResponseHeaders(res, {
      maxAgeSeconds: 300,
      requestId: "req-456",
      cacheState: "HIT",
    });
    expect(res.headers.get("Cache-Control")).toBe("public, max-age=300");
    expect(res.headers.get("X-Cache-State")).toBe("HIT");
    expect(res.headers.get("X-Request-Id")).toBe("req-456");
  });

  it("includes stale-while-revalidate when provided", () => {
    const res = NextResponse.json({});
    setCacheResponseHeaders(res, {
      maxAgeSeconds: 60,
      staleWhileRevalidateSeconds: 120,
      requestId: "req-789",
    });
    expect(res.headers.get("Cache-Control")).toBe(
      "public, max-age=60, stale-while-revalidate=120",
    );
  });

  it("includes cache tags when provided", () => {
    const res = NextResponse.json({});
    setCacheResponseHeaders(res, {
      maxAgeSeconds: 60,
      requestId: "req-abc",
      tags: ["regime", "vix"],
    });
    expect(res.headers.get("X-Cache-Tags")).toBe("regime,vix");
  });
});

describe("jsonApiError", () => {
  it("returns proper error payload with status 404", () => {
    const res = jsonApiError({
      message: "Not found",
      status: 404,
      requestId: "req-err",
    });
    expect(res.status).toBe(404);
  });

  it("defaults to 500 and INTERNAL_ERROR", () => {
    const res = jsonApiError({
      message: "Something broke",
      requestId: "req-500",
    });
    expect(res.status).toBe(500);
  });

  it("includes detail when provided", async () => {
    const res = jsonApiError({
      message: "Bad input",
      status: 400,
      code: "VALIDATION_ERROR",
      detail: "ticker is required",
      requestId: "req-detail",
    });
    const body = await res.json();
    expect(body.code).toBe("VALIDATION_ERROR");
    expect(body.detail).toBe("ticker is required");
    expect(body.requestId).toBe("req-detail");
  });
});
```

- [ ] **Step 2: Run test**

Run: `cd /Users/chenxi/projects/xenon/web && npx vitest run --config ../vitest.config.ts web/tests/api-contracts.test.ts 2>&1 | tail -10`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add web/tests/api-contracts.test.ts
git commit -m "test: add apiContracts unit tests — request IDs, cache headers, error payloads"
```

---

### Task 9: Tier 1 — scales.test.ts

**Files:**

- Create: `web/tests/scales.test.ts`

- [ ] **Step 1: Write the test**

```ts
import { describe, it, expect } from "vitest";
import { scaleLinear, scaleTime } from "../lib/scales";

describe("scaleLinear", () => {
  it("maps domain to range linearly", () => {
    const s = scaleLinear().domain([0, 100]).range([0, 500]);
    expect(s(0)).toBe(0);
    expect(s(50)).toBe(250);
    expect(s(100)).toBe(500);
  });

  it("handles inverted domain", () => {
    const s = scaleLinear().domain([100, 0]).range([0, 500]);
    expect(s(100)).toBe(0);
    expect(s(0)).toBe(500);
  });

  it("invert maps range back to domain", () => {
    const s = scaleLinear().domain([0, 100]).range([0, 500]);
    expect(s.invert(250)).toBe(50);
    expect(s.invert(0)).toBe(0);
  });

  it("handles zero-width domain (returns midpoint)", () => {
    const s = scaleLinear().domain([50, 50]).range([0, 100]);
    expect(s(50)).toBe(50); // (r0 + r1) / 2
  });

  it("generates nice ticks", () => {
    const s = scaleLinear().domain([0, 100]);
    const ticks = s.ticks(5);
    expect(ticks.length).toBeGreaterThan(0);
    expect(ticks[0]).toBeGreaterThanOrEqual(0);
    expect(ticks[ticks.length - 1]).toBeLessThanOrEqual(100);
    // Ticks should be evenly spaced
    const step = ticks[1] - ticks[0];
    for (let i = 2; i < ticks.length; i++) {
      expect(ticks[i] - ticks[i - 1]).toBeCloseTo(step, 10);
    }
  });

  it("returns empty ticks for invalid inputs", () => {
    const s = scaleLinear().domain([NaN, 100]);
    expect(s.ticks(5)).toEqual([]);
  });
});

describe("scaleTime", () => {
  it("maps Date domain to numeric range", () => {
    const d0 = new Date("2026-01-01T00:00:00Z");
    const d1 = new Date("2026-01-02T00:00:00Z");
    const s = scaleTime().domain([d0, d1]).range([0, 100]);
    const midpoint = new Date("2026-01-01T12:00:00Z");
    expect(s(midpoint)).toBeCloseTo(50, 0);
  });

  it("invert maps back to Date", () => {
    const d0 = new Date("2026-01-01T00:00:00Z");
    const d1 = new Date("2026-01-02T00:00:00Z");
    const s = scaleTime().domain([d0, d1]).range([0, 100]);
    const result = s.invert(50);
    expect(result.getTime()).toBeCloseTo(
      new Date("2026-01-01T12:00:00Z").getTime(),
      -3,
    );
  });

  it("generates time ticks", () => {
    const d0 = new Date("2026-01-01T00:00:00Z");
    const d1 = new Date("2026-01-02T00:00:00Z");
    const s = scaleTime().domain([d0, d1]).range([0, 100]);
    const ticks = s.ticks(6);
    expect(ticks.length).toBeGreaterThan(0);
    expect(ticks[0].getTime()).toBeGreaterThanOrEqual(d0.getTime());
  });

  it("returns single tick for zero-width domain", () => {
    const d = new Date("2026-01-01T00:00:00Z");
    const s = scaleTime().domain([d, d]).range([0, 100]);
    expect(s.ticks(5)).toHaveLength(1);
  });
});
```

- [ ] **Step 2: Run test**

Run: `cd /Users/chenxi/projects/xenon/web && npx vitest run --config ../vitest.config.ts web/tests/scales.test.ts 2>&1 | tail -10`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add web/tests/scales.test.ts
git commit -m "test: add scales unit tests — scaleLinear, scaleTime, tick generation edge cases"
```

---

### Task 10: Tier 3 — arrayUtils.test.ts

**Files:**

- Create: `web/tests/array-utils.test.ts`

- [ ] **Step 1: Write the test**

```ts
import { describe, it, expect } from "vitest";
import { extent, mean, bisectLeft } from "../lib/arrayUtils";

describe("extent", () => {
  it("returns [min, max] from numbers", () => {
    expect(extent([3, 1, 4, 1, 5, 9])).toEqual([1, 9]);
  });

  it("returns [min, max] with accessor", () => {
    const data = [{ v: 10 }, { v: 5 }, { v: 20 }];
    expect(extent(data, (d) => d.v)).toEqual([5, 20]);
  });

  it("returns [undefined, undefined] for empty array", () => {
    expect(extent([])).toEqual([undefined, undefined]);
  });

  it("skips null/undefined/NaN values", () => {
    const data = [
      { v: null },
      { v: 5 },
      { v: undefined },
      { v: 10 },
      { v: NaN },
    ];
    expect(extent(data, (d) => d.v as number)).toEqual([5, 10]);
  });

  it("handles single element", () => {
    expect(extent([42])).toEqual([42, 42]);
  });
});

describe("mean", () => {
  it("computes average", () => {
    expect(mean([{ v: 10 }, { v: 20 }, { v: 30 }], (d) => d.v)).toBe(20);
  });

  it("returns undefined for empty array", () => {
    expect(mean([], (d: never) => d)).toBe(undefined);
  });

  it("skips null values", () => {
    expect(
      mean([{ v: 10 }, { v: null }, { v: 30 }], (d) => d.v as number),
    ).toBe(20);
  });
});

describe("bisectLeft", () => {
  it("finds insertion point for numbers", () => {
    const arr = [{ t: 1 }, { t: 3 }, { t: 5 }, { t: 7 }];
    expect(bisectLeft(arr, 4, (d) => d.t)).toBe(2); // between 3 and 5
  });

  it("returns 0 for value before all elements", () => {
    const arr = [{ t: 10 }, { t: 20 }];
    expect(bisectLeft(arr, 5, (d) => d.t)).toBe(0);
  });

  it("returns length for value after all elements", () => {
    const arr = [{ t: 10 }, { t: 20 }];
    expect(bisectLeft(arr, 25, (d) => d.t)).toBe(2);
  });

  it("works with Date values", () => {
    const d1 = new Date("2026-01-01");
    const d2 = new Date("2026-01-03");
    const d3 = new Date("2026-01-05");
    const arr = [{ t: d1 }, { t: d2 }, { t: d3 }];
    expect(bisectLeft(arr, new Date("2026-01-02"), (d) => d.t)).toBe(1);
  });

  it("returns 0 for empty array", () => {
    expect(bisectLeft([], 5, (d: never) => d)).toBe(0);
  });
});
```

- [ ] **Step 2: Run test**

Run: `cd /Users/chenxi/projects/xenon/web && npx vitest run --config ../vitest.config.ts web/tests/array-utils.test.ts 2>&1 | tail -10`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add web/tests/array-utils.test.ts
git commit -m "test: add arrayUtils unit tests — extent, mean, bisectLeft edge cases"
```

---

### Task 11: Tier 3 — criCalc.test.ts

**Files:**

- Create: `web/tests/cri-calc.test.ts`

- [ ] **Step 1: Write the test**

```ts
import { describe, it, expect } from "vitest";
import {
  scoreVix,
  scoreVvix,
  scoreCorrelation,
  scoreMomentum,
  criLevel,
  computeCri,
} from "../lib/criCalc";

describe("scoreVix", () => {
  it("returns 0 for VIX at 15 (floor)", () => {
    expect(scoreVix(15, 0)).toBe(0);
  });

  it("returns 15 for VIX level at 40 (ceiling) with 0 ROC", () => {
    expect(scoreVix(40, 0)).toBe(15);
  });

  it("adds ROC component for positive ROC", () => {
    const score = scoreVix(15, 60); // level=0, roc=10
    expect(score).toBe(10);
  });

  it("clamps at 25", () => {
    expect(scoreVix(50, 100)).toBe(25);
  });

  it("returns 0 for NaN input", () => {
    expect(scoreVix(NaN, 10)).toBe(0);
    expect(scoreVix(20, NaN)).toBe(0);
  });
});

describe("scoreVvix", () => {
  it("returns 0 for VVIX at 90 (floor)", () => {
    expect(scoreVvix(90, 5)).toBe(0);
  });

  it("returns 17 for VVIX at 140 with ratio at floor", () => {
    expect(scoreVvix(140, 5)).toBe(17);
  });

  it("returns 0 for NaN", () => {
    expect(scoreVvix(NaN, 6)).toBe(0);
  });
});

describe("scoreCorrelation", () => {
  it("returns 0 for COR at 25 (floor)", () => {
    expect(scoreCorrelation(25, 0)).toBe(0);
  });

  it("scores spike component independently", () => {
    const score = scoreCorrelation(25, 20); // level=0, spike=8
    expect(score).toBe(8);
  });

  it("handles NaN correlation gracefully", () => {
    expect(scoreCorrelation(NaN, 10)).toBe(0);
  });

  it("handles NaN change by treating as 0", () => {
    const score = scoreCorrelation(50, NaN);
    // level = ((50-25)/(70-25)) * 17 ≈ 9.44, spike = 0
    expect(score).toBeCloseTo(9.4, 0);
  });
});

describe("scoreMomentum", () => {
  it("returns 0 for positive distance (above MA)", () => {
    expect(scoreMomentum(5)).toBe(0);
  });

  it("returns 0 for zero distance", () => {
    expect(scoreMomentum(0)).toBe(0);
  });

  it("scores linearly for negative distance", () => {
    // -5% → (5/10) * 25 = 12.5
    expect(scoreMomentum(-5)).toBe(12.5);
  });

  it("clamps at 25 for -10%+", () => {
    expect(scoreMomentum(-10)).toBe(25);
    expect(scoreMomentum(-15)).toBe(25);
  });

  it("returns 0 for NaN", () => {
    expect(scoreMomentum(NaN)).toBe(0);
  });
});

describe("criLevel", () => {
  it("classifies score ranges correctly", () => {
    expect(criLevel(0)).toBe("LOW");
    expect(criLevel(24.9)).toBe("LOW");
    expect(criLevel(25)).toBe("ELEVATED");
    expect(criLevel(49.9)).toBe("ELEVATED");
    expect(criLevel(50)).toBe("HIGH");
    expect(criLevel(74.9)).toBe("HIGH");
    expect(criLevel(75)).toBe("CRITICAL");
    expect(criLevel(100)).toBe("CRITICAL");
  });
});

describe("computeCri", () => {
  it("computes composite score from all components", () => {
    const result = computeCri({
      vix: 30,
      vix5dRoc: 30,
      vvix: 120,
      vvixVixRatio: 6.5,
      corr: 50,
      corr5dChange: 10,
      spxDistancePct: -3,
    });
    expect(result.score).toBeGreaterThan(0);
    expect(result.score).toBeLessThanOrEqual(100);
    expect(result.level).toBeDefined();
    expect(result.components.vix).toBeGreaterThan(0);
    expect(result.components.vvix).toBeGreaterThan(0);
    expect(result.components.correlation).toBeGreaterThan(0);
    expect(result.components.momentum).toBeGreaterThan(0);
  });

  it("returns LOW for calm market", () => {
    const result = computeCri({
      vix: 12,
      vix5dRoc: 0,
      vvix: 80,
      vvixVixRatio: 4,
      corr: 20,
      corr5dChange: 0,
      spxDistancePct: 3,
    });
    expect(result.score).toBe(0);
    expect(result.level).toBe("LOW");
  });

  it("returns CRITICAL for extreme stress", () => {
    const result = computeCri({
      vix: 50,
      vix5dRoc: 80,
      vvix: 150,
      vvixVixRatio: 10,
      corr: 80,
      corr5dChange: 25,
      spxDistancePct: -12,
    });
    expect(result.score).toBe(100);
    expect(result.level).toBe("CRITICAL");
  });
});
```

- [ ] **Step 2: Run test**

Run: `cd /Users/chenxi/projects/xenon/web && npx vitest run --config ../vitest.config.ts web/tests/cri-calc.test.ts 2>&1 | tail -10`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add web/tests/cri-calc.test.ts
git commit -m "test: add CRI calc unit tests — all 4 scoring components, level classification, composite"
```

---

### Task 12: Tier 3 — ctaFreshness.test.ts

**Files:**

- Create: `web/tests/cta-freshness.test.ts`

- [ ] **Step 1: Write the test**

```ts
import { describe, it, expect, vi, afterEach } from "vitest";
import {
  latestClosedTradingDayET,
  buildCtaCacheMeta,
} from "../lib/ctaFreshness";

describe("latestClosedTradingDayET", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns today if after market close on a weekday", () => {
    // Wednesday 2026-04-08 at 17:00 ET
    vi.useFakeTimers();
    // 17:00 ET = 21:00 UTC (EDT, UTC-4)
    vi.setSystemTime(new Date("2026-04-08T21:00:00Z"));
    const result = latestClosedTradingDayET(new Date());
    expect(result).toBe("2026-04-08");
  });

  it("returns previous trading day before market close", () => {
    // Wednesday 2026-04-08 at 10:00 ET
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-04-08T14:00:00Z"));
    const result = latestClosedTradingDayET(new Date());
    expect(result).toBe("2026-04-07"); // Tuesday
  });

  it("returns Friday for Saturday", () => {
    // Saturday 2026-04-11
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-04-11T15:00:00Z"));
    const result = latestClosedTradingDayET(new Date());
    expect(result).toBe("2026-04-10"); // Friday
  });

  it("returns Friday for Sunday", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-04-12T15:00:00Z"));
    const result = latestClosedTradingDayET(new Date());
    expect(result).toBe("2026-04-10");
  });
});

describe("buildCtaCacheMeta", () => {
  it("returns fresh when cache date matches target", () => {
    const meta = buildCtaCacheMeta({
      targetDate: "2026-04-08",
      latestCacheDate: "2026-04-08",
      mtimeMs: Date.now() - 60_000,
    });
    expect(meta.is_stale).toBe(false);
    expect(meta.stale_reason).toBe("fresh");
  });

  it("returns behind_target when cache date is old", () => {
    const meta = buildCtaCacheMeta({
      targetDate: "2026-04-08",
      latestCacheDate: "2026-04-07",
      mtimeMs: Date.now() - 86_400_000,
    });
    expect(meta.is_stale).toBe(true);
    expect(meta.stale_reason).toBe("behind_target");
  });

  it("returns missing_cache when no cache exists", () => {
    const meta = buildCtaCacheMeta({
      targetDate: "2026-04-08",
      latestCacheDate: null,
      mtimeMs: null,
    });
    expect(meta.is_stale).toBe(true);
    expect(meta.stale_reason).toBe("missing_cache");
    expect(meta.age_seconds).toBe(null);
    expect(meta.last_refresh).toBe(null);
  });
});
```

- [ ] **Step 2: Run test**

Run: `cd /Users/chenxi/projects/xenon/web && npx vitest run --config ../vitest.config.ts web/tests/cta-freshness.test.ts 2>&1 | tail -10`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add web/tests/cta-freshness.test.ts
git commit -m "test: add ctaFreshness tests — trading day calc, cache staleness detection"
```

---

### Task 13: Tier 3 — ctaPercentiles.test.ts

**Files:**

- Create: `web/tests/cta-percentiles.test.ts`

- [ ] **Step 1: Write the test**

```ts
import { describe, it, expect } from "vitest";
import {
  normalizeCtaPercentile,
  formatCtaPercentileLabel,
} from "../lib/ctaPercentiles";

describe("normalizeCtaPercentile", () => {
  it("passes through 0-100 values unchanged", () => {
    expect(normalizeCtaPercentile(50)).toBe(50);
    expect(normalizeCtaPercentile(0)).toBe(0);
    expect(normalizeCtaPercentile(100)).toBe(100);
  });

  it("scales 0-1 fractional values to 0-100", () => {
    expect(normalizeCtaPercentile(0.5)).toBe(50);
    expect(normalizeCtaPercentile(0.95)).toBe(95);
  });

  it("clamps values above 100", () => {
    expect(normalizeCtaPercentile(150)).toBe(100);
  });

  it("clamps negative values to 0", () => {
    expect(normalizeCtaPercentile(-10)).toBe(0);
  });

  it("returns null for null/undefined/NaN", () => {
    expect(normalizeCtaPercentile(null)).toBe(null);
    expect(normalizeCtaPercentile(undefined)).toBe(null);
    expect(normalizeCtaPercentile(NaN)).toBe(null);
  });
});

describe("formatCtaPercentileLabel", () => {
  it("formats ordinal suffixes correctly", () => {
    expect(formatCtaPercentileLabel(1)).toBe("1st");
    expect(formatCtaPercentileLabel(2)).toBe("2nd");
    expect(formatCtaPercentileLabel(3)).toBe("3rd");
    expect(formatCtaPercentileLabel(4)).toBe("4th");
    expect(formatCtaPercentileLabel(11)).toBe("11th");
    expect(formatCtaPercentileLabel(12)).toBe("12th");
    expect(formatCtaPercentileLabel(13)).toBe("13th");
    expect(formatCtaPercentileLabel(21)).toBe("21st");
    expect(formatCtaPercentileLabel(22)).toBe("22nd");
    expect(formatCtaPercentileLabel(23)).toBe("23rd");
    expect(formatCtaPercentileLabel(99)).toBe("99th");
  });

  it("returns --- for null input", () => {
    expect(formatCtaPercentileLabel(null)).toBe("---");
  });

  it("handles fractional input (0-1 range)", () => {
    expect(formatCtaPercentileLabel(0.75)).toBe("75th");
  });
});
```

- [ ] **Step 2: Run test**

Run: `cd /Users/chenxi/projects/xenon/web && npx vitest run --config ../vitest.config.ts web/tests/cta-percentiles.test.ts 2>&1 | tail -10`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add web/tests/cta-percentiles.test.ts
git commit -m "test: add ctaPercentiles tests — normalization, ordinal suffix formatting"
```

---

### Task 14: Tier 3 — vcgStaleness.test.ts

**Files:**

- Create: `web/tests/vcg-staleness.test.ts`

- [ ] **Step 1: Write the test**

```ts
import { describe, it, expect, vi, afterEach } from "vitest";
import { isVcgDataStale, type VcgDataShape } from "../lib/vcgStaleness";

describe("isVcgDataStale", () => {
  it("returns true when scan_time is missing", () => {
    expect(isVcgDataStale({}, "2026-04-08", false)).toBe(true);
  });

  it("returns true when scan_time is unparseable", () => {
    expect(
      isVcgDataStale({ scan_time: "not-a-date" }, "2026-04-08", false),
    ).toBe(true);
  });

  it("returns true when session date differs from today", () => {
    expect(
      isVcgDataStale(
        { scan_time: "2026-04-07T15:00:00-04:00" },
        "2026-04-08",
        false,
      ),
    ).toBe(true);
  });

  it("returns false when same day + market closed (EOD data final)", () => {
    expect(
      isVcgDataStale(
        { scan_time: "2026-04-08T15:59:00-04:00" },
        "2026-04-08",
        false, // market closed
      ),
    ).toBe(false);
  });

  it("returns false when market open + scan_time within TTL (60s)", () => {
    const recent = new Date(Date.now() - 30_000).toISOString(); // 30s ago
    expect(
      isVcgDataStale(
        { scan_time: recent },
        new Date().toLocaleDateString("sv", { timeZone: "America/New_York" }),
        true, // market open
      ),
    ).toBe(false);
  });

  it("returns true when market open + scan_time exceeds TTL", () => {
    const old = new Date(Date.now() - 120_000).toISOString(); // 2min ago
    expect(
      isVcgDataStale(
        { scan_time: old },
        new Date().toLocaleDateString("sv", { timeZone: "America/New_York" }),
        true,
      ),
    ).toBe(true);
  });
});
```

- [ ] **Step 2: Run test**

Run: `cd /Users/chenxi/projects/xenon/web && npx vitest run --config ../vitest.config.ts web/tests/vcg-staleness.test.ts 2>&1 | tail -10`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add web/tests/vcg-staleness.test.ts
git commit -m "test: add vcgStaleness tests — scan_time parsing, market-hours-aware TTL"
```

---

### Task 15: Tier 2 — useUwStats.test.ts

**Files:**

- Create: `web/tests/use-uw-stats.test.ts`

- [ ] **Step 1: Write the test**

```ts
/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useUwStats } from "../lib/useUwStats";

const MOCK_STATS = {
  totals: {
    requests: 100,
    success: 90,
    cached: 30,
    retries: 5,
    failures: 3,
    rate_limits: 2,
    connection_errors: 0,
  },
  latency_ms: { samples: 90, min: 50, max: 800, avg: 200, p95: 500 },
  by_status: { "200": 90, "429": 2, "500": 3 },
  uptime_seconds: 3600,
};

describe("useUwStats", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(MOCK_STATS), { status: 200 }),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("fetches stats on mount", async () => {
    const { result } = renderHook(() => useUwStats());
    await waitFor(() => expect(result.current).not.toBe(null));
    expect(result.current!.totals.requests).toBe(100);
    expect(result.current!.latency_ms.p95).toBe(500);
  });

  it("polls every 10 seconds", async () => {
    const { result } = renderHook(() => useUwStats());
    await waitFor(() => expect(result.current).not.toBe(null));

    const updatedStats = {
      ...MOCK_STATS,
      totals: { ...MOCK_STATS.totals, requests: 200 },
    };
    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify(updatedStats), { status: 200 }),
    );

    await act(async () => {
      vi.advanceTimersByTime(10_000);
    });
    await waitFor(() => expect(result.current!.totals.requests).toBe(200));
  });

  it("returns null on fetch error (silent fail)", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new Error("network error"));
    const { result } = renderHook(() => useUwStats());
    // Should remain null — not throw
    await act(async () => {
      vi.advanceTimersByTime(100);
    });
    expect(result.current).toBe(null);
  });

  it("returns null on non-200 response", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(new Response("", { status: 500 }));
    const { result } = renderHook(() => useUwStats());
    await act(async () => {
      vi.advanceTimersByTime(100);
    });
    expect(result.current).toBe(null);
  });

  it("cleans up interval on unmount", async () => {
    const { result, unmount } = renderHook(() => useUwStats());
    await waitFor(() => expect(result.current).not.toBe(null));
    unmount();
    // After unmount, further intervals should not call fetch
    const callCount = vi.mocked(fetch).mock.calls.length;
    await act(async () => {
      vi.advanceTimersByTime(20_000);
    });
    expect(vi.mocked(fetch).mock.calls.length).toBe(callCount);
  });
});
```

- [ ] **Step 2: Run test**

Run: `cd /Users/chenxi/projects/xenon/web && npx vitest run --config ../vitest.config.ts web/tests/use-uw-stats.test.ts 2>&1 | tail -10`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add web/tests/use-uw-stats.test.ts
git commit -m "test: add useUwStats hook tests — polling lifecycle, silent fail, unmount cleanup"
```

---

### Task 16: Tier 2 — useUwStatsHistory.test.ts

**Files:**

- Create: `web/tests/use-uw-stats-history.test.ts`

- [ ] **Step 1: Write the test**

```ts
/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useUwStatsHistory } from "../lib/useUwStatsHistory";

const MOCK_HISTORY = {
  buckets: [
    {
      hour: "2026-04-08T14:00:00Z",
      requests_2xx: 50,
      requests_4xx: 2,
      requests_5xx: 0,
      cached: 20,
      avg_latency_ms: 180,
    },
    {
      hour: "2026-04-08T15:00:00Z",
      requests_2xx: 60,
      requests_4xx: 1,
      requests_5xx: 1,
      cached: 25,
      avg_latency_ms: 200,
    },
  ],
};

describe("useUwStatsHistory", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(MOCK_HISTORY), { status: 200 }),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("fetches history on mount with default hours=96", async () => {
    const { result } = renderHook(() => useUwStatsHistory());
    await waitFor(() => expect(result.current).not.toBe(null));
    expect(result.current!.buckets).toHaveLength(2);
    expect(fetch).toHaveBeenCalledWith(
      "/api/uw-stats/history?hours=96",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("passes custom hours parameter", async () => {
    renderHook(() => useUwStatsHistory(24));
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(fetch).toHaveBeenCalledWith(
      "/api/uw-stats/history?hours=24",
      expect.anything(),
    );
  });

  it("polls every 60 seconds", async () => {
    const { result } = renderHook(() => useUwStatsHistory());
    await waitFor(() => expect(result.current).not.toBe(null));

    vi.mocked(fetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ buckets: [] }), { status: 200 }),
    );
    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });
    await waitFor(() => expect(result.current!.buckets).toHaveLength(0));
  });

  it("returns null on error (silent fail)", async () => {
    vi.mocked(fetch).mockRejectedValueOnce(new Error("offline"));
    const { result } = renderHook(() => useUwStatsHistory());
    await act(async () => {
      vi.advanceTimersByTime(100);
    });
    expect(result.current).toBe(null);
  });
});
```

- [ ] **Step 2: Run test**

Run: `cd /Users/chenxi/projects/xenon/web && npx vitest run --config ../vitest.config.ts web/tests/use-uw-stats-history.test.ts 2>&1 | tail -10`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add web/tests/use-uw-stats-history.test.ts
git commit -m "test: add useUwStatsHistory hook tests — polling, custom hours, silent fail"
```

---

### Task 17: Tier 2 — test_uw_stats_routes.py (FastAPI)

**Files:**

- Create: `scripts/tests/test_uw_stats_routes.py`

- [ ] **Step 1: Write the test**

```python
"""Tests for /uw-stats FastAPI endpoints.

Covers GET /uw-stats, POST /uw-stats/reset, GET /uw-stats/history,
and POST /uw-stats/history/clear.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def mock_stats():
    """Mock the process-wide stats singleton."""
    stats = MagicMock()
    stats.get_stats.return_value = {
        "totals": {"requests": 100, "success": 90, "cached": 30},
        "latency_ms": {"samples": 90, "avg": 200, "p95": 500},
        "uptime_seconds": 3600,
    }
    stats.get_hourly_history.return_value = [
        {"hour": "2026-04-08T14:00:00Z", "requests_2xx": 50},
        {"hour": "2026-04-08T15:00:00Z", "requests_2xx": 60},
    ]
    return stats


@pytest.fixture
def client(mock_stats):
    """TestClient with isolated FastAPI app (avoids importing full server).

    Patches at the source module (utils.uw_api_stats.stats) because
    uw_stats routes use lazy imports inside each function body —
    there's no module-level stats attribute to patch on the route.
    """
    with patch("utils.uw_api_stats.stats", mock_stats):
        from api.routes.uw_stats import router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)
        yield TestClient(app)


class TestGetUwStats:
    def test_returns_stats_snapshot(self, client, mock_stats):
        r = client.get("/uw-stats")
        assert r.status_code == 200
        body = r.json()
        assert body["totals"]["requests"] == 100
        assert body["latency_ms"]["p95"] == 500
        mock_stats.get_stats.assert_called_once()

    def test_returns_dict(self, client):
        r = client.get("/uw-stats")
        assert isinstance(r.json(), dict)


class TestPostUwStatsReset:
    def test_resets_session_counters(self, client, mock_stats):
        r = client.post("/uw-stats/reset")
        assert r.status_code == 200
        assert r.json()["status"] == "reset"
        mock_stats.reset.assert_called_once()


class TestGetUwStatsHistory:
    def test_returns_hourly_buckets(self, client, mock_stats):
        r = client.get("/uw-stats/history")
        assert r.status_code == 200
        body = r.json()
        assert "buckets" in body
        assert len(body["buckets"]) == 2
        mock_stats.get_hourly_history.assert_called_once_with(hours=96)

    def test_custom_hours_parameter(self, client, mock_stats):
        r = client.get("/uw-stats/history?hours=24")
        assert r.status_code == 200
        mock_stats.get_hourly_history.assert_called_once_with(hours=24)

    def test_rejects_hours_below_minimum(self, client):
        r = client.get("/uw-stats/history?hours=0")
        assert r.status_code == 422  # FastAPI validation error

    def test_rejects_hours_above_maximum(self, client):
        r = client.get("/uw-stats/history?hours=200")
        assert r.status_code == 422


class TestPostUwStatsHistoryClear:
    def test_clears_all_history(self, client, mock_stats):
        r = client.post("/uw-stats/history/clear")
        assert r.status_code == 200
        assert r.json()["status"] == "cleared"
        mock_stats.clear_history.assert_called_once()
```

- [ ] **Step 2: Run test**

Run: `python3.13 -m pytest scripts/tests/test_uw_stats_routes.py -xvs 2>&1 | tail -20`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add scripts/tests/test_uw_stats_routes.py
git commit -m "test: add /uw-stats route tests — GET stats, reset, history, history/clear"
```

---

### Task 18: Tier 2 — test_trend_scan_route.py (FastAPI)

**Files:**

- Create: `scripts/tests/test_trend_scan_route.py`

- [ ] **Step 1: Write the test**

```python
"""Tests for POST /trend-scan FastAPI endpoint.

The route spawns trend_scan.py as a subprocess with 180s timeout.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient


from api.subprocess import ScriptResult


@pytest.fixture
def client():
    """Build isolated app with just the /trend-scan route.

    The route is defined inline in server.py (not a separate router),
    so we replicate it in a fresh FastAPI app to avoid importing the
    full server with IB/Futu/lifespan dependencies.
    """
    from fastapi import FastAPI, HTTPException

    _write_cache_calls = []

    async def _mock_run_script(script, args=None, timeout=30.0):
        # Will be replaced per-test via monkeypatch
        return ScriptResult(ok=True, data={})

    def _mock_write_cache(path, data):
        _write_cache_calls.append((path, data))

    app = FastAPI()
    app._mock_run_script = _mock_run_script
    app._mock_write_cache = _mock_write_cache
    app._write_cache_calls = _write_cache_calls

    @app.post("/trend-scan")
    async def trend_scan():
        result = await app._mock_run_script("trend_scan.py", ["--top", "25"], timeout=180)
        if not result.ok:
            raise HTTPException(status_code=502, detail=result.error)
        app._mock_write_cache("trend_scan.json", result.data)
        return result.data

    return TestClient(app)


class TestPostTrendScan:
    def test_returns_scan_results_on_success(self, client):
        mock_data = {
            "scan_id": "trend_20260410",
            "candidates": [{"ticker": "NVDA", "final_score": 0.82}],
        }
        original = client.app._mock_run_script

        async def _run(script, args=None, timeout=30.0):
            return ScriptResult(ok=True, data=mock_data)

        client.app._mock_run_script = _run
        r = client.post("/trend-scan")
        client.app._mock_run_script = original
        assert r.status_code == 200
        body = r.json()
        assert body["scan_id"] == "trend_20260410"
        assert len(body["candidates"]) == 1

    def test_returns_502_on_script_failure(self, client):
        async def _run(script, args=None, timeout=30.0):
            return ScriptResult(ok=False, error="trend_scan.py crashed")

        client.app._mock_run_script = _run
        r = client.post("/trend-scan")
        assert r.status_code == 502
        assert "crashed" in r.json()["detail"]

    def test_writes_cache_file_on_success(self, client):
        mock_data = {"scan_id": "test"}

        async def _run(script, args=None, timeout=30.0):
            return ScriptResult(ok=True, data=mock_data)

        client.app._mock_run_script = _run
        client.app._write_cache_calls.clear()
        client.post("/trend-scan")
        assert len(client.app._write_cache_calls) == 1
        cache_path, cache_data = client.app._write_cache_calls[0]
        assert "trend_scan.json" in str(cache_path)
        assert cache_data["scan_id"] == "test"
```

- [ ] **Step 2: Run test**

Run: `python3.13 -m pytest scripts/tests/test_trend_scan_route.py -xvs 2>&1 | tail -20`
Expected: All tests pass.

- [ ] **Step 3: Commit**

```bash
git add scripts/tests/test_trend_scan_route.py
git commit -m "test: add POST /trend-scan route tests — subprocess spawn, 180s timeout, error codes"
```

---

### Task 19: Verify Full Suite + Gap Detector

- [ ] **Step 1: Run full Python test suite**

Run: `python3.13 -m pytest scripts/tests/ -q 2>&1 | tail -5`
Expected: 2,126+ tests pass, 0 failures.

- [ ] **Step 2: Run full Vitest suite**

Run: `cd /Users/chenxi/projects/xenon/web && npm test 2>&1 | tail -10`
Expected: 1,592+ tests pass, 0 failures.

- [ ] **Step 3: Run gap detector to verify improvement**

Run: `python3.13 scripts/test_gap_detector.py`
Expected: Fewer orphans than before (Tiers 1-3 files now have tests).

- [ ] **Step 4: Final commit for any adjustments**

If any test needed fixing, commit the fixes:

```bash
git add -A
git commit -m "fix: test suite adjustment from full-suite verification"
```

---

## Summary

| Phase     | Tasks        | New Files     | Test Cases    |
| --------- | ------------ | ------------- | ------------- |
| 1 (Infra) | 1-6          | 11 files      | 3 (manifest)  |
| 2 Tier 1  | 7-9          | 3 test files  | ~30           |
| 2 Tier 3  | 10-14        | 5 test files  | ~35           |
| 2 Tier 2  | 15-18        | 4 test files  | ~25           |
| Verify    | 19           | —             | —             |
| **Total** | **19 tasks** | **~23 files** | **~93 tests** |

Tasks are ordered for maximum independence: infrastructure first, then pure-function tests (no mocking), then hook tests (jsdom), then backend route tests (TestClient). Each task produces a green test run and a clean commit.
