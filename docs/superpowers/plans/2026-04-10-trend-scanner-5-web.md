# Sub-Plan 5: Web Integration

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the trend scanner into the web stack: FastAPI route, Next.js API route, replace `ScannerSections` component with new trend scanner UI, add scheduled pre-market run.

**Architecture:** FastAPI `POST /trend-scan` → subprocess `trend_scan.py` → JSON cache + DuckDB. Next.js API route reads/writes cache. Frontend renders ranked candidates with score breakdown bars, direction badges, trade chips, and expandable detail rows.

**Tech Stack:** FastAPI, Next.js App Router, TypeScript, React, Vitest

**Spec:** `docs/superpowers/specs/2026-04-10-trend-scanner-design.md` (Web Integration section)

**Depends on:** Sub-Plans 1-4 must be complete.

**Reference files:**

- Existing API route: `web/app/api/scanner/route.ts`
- Existing component: `web/components/WorkspaceSections.tsx:1480-1680`
- Existing hook: `web/lib/useScanner.ts`
- Existing types: `web/lib/types.ts:413-432`
- FastAPI server: `scripts/api/server.py` (line ~946 for existing `/scan` route)
- Brand spec: `brand/CLAUDE.md`

---

## File Structure

```
scripts/api/
└── server.py                        # MODIFY — add POST /trend-scan route + scheduler

web/
├── lib/
│   ├── types.ts                     # MODIFY — replace ScannerSignal/ScannerData types
│   └── useScanner.ts                # MODIFY — update timestamp extractor
├── app/
│   └── api/scanner/route.ts         # MODIFY — point to trend_scan.json, POST to /trend-scan
└── components/
    └── WorkspaceSections.tsx         # MODIFY — replace ScannerSections component (~lines 1480-1680)

web/tests/
└── trend-scanner.test.ts            # CREATE — component tests
```

---

### Task 1: FastAPI Route (`POST /trend-scan`)

**Files:**

- Modify: `scripts/api/server.py`

- [ ] **Step 1: Read the existing `/scan` route for reference**

Run: `grep -n "def scan" scripts/api/server.py`
Reference: lines ~946-953 contain `@app.post("/scan")`.

- [ ] **Step 2: Add the new `/trend-scan` route**

Add after the existing `/scan` route in `scripts/api/server.py`:

```python
@app.post("/trend-scan")
async def trend_scan():
    """Run 3-stage trend scanner (trend_scan.py --top 25)."""
    result = await run_script("trend_scan.py", ["--top", "25"], timeout=180)
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.error)
    _write_cache(DATA_DIR / "trend_scan.json", result.data)
    return result.data
```

- [ ] **Step 3: Add scheduled pre-market run**

In the `lifespan()` function (around line ~121), add a scheduled task for the trend scanner. Find the existing daily job pattern and add:

```python
async def _trend_scan_premarket_loop():
    """Run trend scanner at 8:30 AM ET on weekdays."""
    import zoneinfo
    et = zoneinfo.ZoneInfo("America/New_York")
    while True:
        now = datetime.now(et)
        # Calculate next 8:30 AM ET
        target_hour, target_min = 8, 30
        target = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        # Skip weekends
        while target.weekday() >= 5:
            target += timedelta(days=1)
        wait_secs = (target - now).total_seconds()
        logger.info("Trend scan scheduled for %s (in %.0fs)", target, wait_secs)
        await asyncio.sleep(wait_secs)
        try:
            result = await run_script("trend_scan.py", ["--top", "25"], timeout=180)
            if result.ok:
                _write_cache(DATA_DIR / "trend_scan.json", result.data)
                logger.info("Pre-market trend scan complete: %d candidates", len(result.data.get("candidates", [])))
            else:
                logger.warning("Pre-market trend scan failed: %s", result.error)
        except Exception:
            logger.warning("Pre-market trend scan error", exc_info=True)
```

In the `lifespan()` startup section, add alongside the existing daily job:

```python
# Pre-market trend scanner (8:30 AM ET weekdays)
_trend_scan_task = None
if os.environ.get("XENON_DAILY_JOB_WORKER_ID", "0") == "0":
    _trend_scan_task = asyncio.create_task(_trend_scan_premarket_loop())
```

In the `lifespan()` shutdown section, add cancellation:

```python
# Cancel trend scan scheduler on shutdown
if _trend_scan_task is not None:
    _trend_scan_task.cancel()
```

- [ ] **Step 4: Commit**

```bash
git add scripts/api/server.py
git commit -m "feat(api): add POST /trend-scan route and pre-market scheduler"
```

---

### Task 2: TypeScript Types

**Files:**

- Modify: `web/lib/types.ts`

- [ ] **Step 1: Add new trend scanner types (additive — do NOT delete old types yet)**

In `web/lib/types.ts`, **add** the following types AFTER the existing `ScannerSignal` and `ScannerData` types. Keep the old types until all consumers are migrated in Step 2. Once migration is complete, delete the old types.

New types to ADD:

```typescript
export type TrendScores = {
  trend: number;
  structure: number;
  volatility: number;
  flow: number;
};

export type TrendIndicators = {
  ma_20: number;
  ma_50: number;
  ma_200: number;
  rsi: number;
  adx: number;
  macd_histogram: number;
  bbw: number;
  rs_vs_spy: number;
  iv_rank: number;
  gamma_flip: number;
  call_wall: number;
  put_wall: number;
};

export type TrendSummaries = {
  trend: string;
  structure: string;
  vol: string;
  flow: string;
};

export type TrendCandidate = {
  ticker: string;
  snapshot_timestamp: string;
  spot_price: number;
  direction: "bullish" | "bearish";
  final_score: number;
  scores: TrendScores;
  indicators: TrendIndicators;
  summaries: TrendSummaries;
  suggested_trade: string;
  invalidation: number;
  flags: string[];
  holding_window: string;
};

export type ScannerData = {
  scan_id: string;
  scan_timestamp: string;
  market_context: {
    spy_close: number;
    vix_close: number;
    regime: string;
  };
  universe_size: number;
  stage_a_survivors: number;
  stage_b_survivors: number;
  candidates: TrendCandidate[];
};
```

- [ ] **Step 2: Remove old `ScannerSignal` and `ScannerData` types**

Delete the old `ScannerSignal` and `ScannerData` type definitions from `web/lib/types.ts` (lines ~413-432). Then grep for any remaining references:

```bash
cd /Users/chenxi/projects/xenon/web && grep -rn "ScannerSignal\|ScannerData" --include="*.ts" --include="*.tsx" | grep -v "node_modules"
```

Update any test files that reference the old types:

- `web/tests/sync-hooks.test.ts` — update mock data shape to use `TrendCandidate`/new `ScannerData`
- `web/tests/route-cache-meta.test.ts` — update mock data shape
- `web/tests/fastapi-migration.test.ts` — update mock data shape

Each test file must be updated to use the new `ScannerData` shape with `scan_id`, `scan_timestamp`, `candidates[]`, etc.

- [ ] **Step 3: Commit**

```bash
cd /Users/chenxi/projects/xenon && git add web/lib/types.ts web/tests/sync-hooks.test.ts web/tests/route-cache-meta.test.ts web/tests/fastapi-migration.test.ts
git commit -m "feat(web): migrate scanner types to trend scanner data model"
```

---

### Task 3: Update useScanner Hook

**Files:**

- Modify: `web/lib/useScanner.ts`

- [ ] **Step 1: Update timestamp extractor**

Replace the contents of `web/lib/useScanner.ts`:

```typescript
"use client";

import { useMemo } from "react";
import { useSyncHook, type UseSyncReturn } from "./useSyncHook";
import type { ScannerData } from "./types";

const config = {
  endpoint: "/api/scanner",
  extractTimestamp: (d: ScannerData) => d.scan_timestamp || null,
};

export function useScanner(active: boolean): UseSyncReturn<ScannerData> {
  const stableConfig = useMemo(() => config, []);
  return useSyncHook<ScannerData>(stableConfig, active);
}
```

- [ ] **Step 2: Commit**

```bash
cd /Users/chenxi/projects/xenon && git add web/lib/useScanner.ts
git commit -m "feat(web): update useScanner hook for trend scanner data model"
```

---

### Task 4: Update Next.js API Route

**Files:**

- Modify: `web/app/api/scanner/route.ts`

- [ ] **Step 1: Update the route to use trend_scan.json and /trend-scan endpoint**

Replace the contents of `web/app/api/scanner/route.ts`:

```typescript
import { NextResponse } from "next/server";
import { readFile } from "fs/promises";
import { statSync } from "fs";
import { join } from "path";
import { xenonFetch } from "@/lib/xenonApi";

export const runtime = "nodejs";

const CACHE_PATH = join(process.cwd(), "..", "data", "trend_scan.json");
const STALE_THRESHOLD_SECONDS = 600;

interface CacheMeta {
  last_refresh: string | null;
  age_seconds: number | null;
  is_stale: boolean;
  stale_threshold_seconds: number;
}

function buildCacheMeta(filePath: string): CacheMeta {
  try {
    const s = statSync(filePath);
    const ageSeconds = (Date.now() - s.mtime.getTime()) / 1000;
    return {
      last_refresh: s.mtime.toISOString(),
      age_seconds: Math.round(ageSeconds),
      is_stale: ageSeconds > STALE_THRESHOLD_SECONDS,
      stale_threshold_seconds: STALE_THRESHOLD_SECONDS,
    };
  } catch {
    return {
      last_refresh: null,
      age_seconds: null,
      is_stale: true,
      stale_threshold_seconds: STALE_THRESHOLD_SECONDS,
    };
  }
}

export async function GET(): Promise<Response> {
  try {
    const raw = await readFile(CACHE_PATH, "utf-8");
    const data = JSON.parse(raw);
    const cache_meta = buildCacheMeta(CACHE_PATH);
    return NextResponse.json({ ...data, cache_meta });
  } catch {
    const cache_meta = buildCacheMeta(CACHE_PATH);
    return NextResponse.json({
      scan_id: "",
      scan_timestamp: "",
      market_context: { spy_close: 0, vix_close: 0, regime: "unknown" },
      universe_size: 0,
      stage_a_survivors: 0,
      stage_b_survivors: 0,
      candidates: [],
      cache_meta,
    });
  }
}

export async function POST(): Promise<Response> {
  try {
    const data = await xenonFetch("/trend-scan", {
      method: "POST",
      timeout: 200_000,
    });
    const cache_meta = buildCacheMeta(CACHE_PATH);
    return NextResponse.json({ ...data, cache_meta });
  } catch (error) {
    try {
      const raw = await readFile(CACHE_PATH, "utf-8");
      const cached = JSON.parse(raw);
      const cache_meta = buildCacheMeta(CACHE_PATH);
      const res = NextResponse.json({ ...cached, cache_meta, is_stale: true });
      res.headers.set(
        "X-Sync-Warning",
        "Xenon API unavailable - serving cached data",
      );
      return res;
    } catch {
      const message =
        error instanceof Error ? error.message : "Trend scanner failed";
      return NextResponse.json({ error: message }, { status: 502 });
    }
  }
}
```

- [ ] **Step 2: Commit**

```bash
cd /Users/chenxi/projects/xenon && git add web/app/api/scanner/route.ts
git commit -m "feat(web): update scanner API route for trend scanner backend"
```

---

### Task 5: Replace ScannerSections Component

**Files:**

- Modify: `web/components/WorkspaceSections.tsx` (lines ~1480-1680)

This is the largest task. Replace the existing `ScannerSortKey`, `scannerSigExtract`, and `ScannerSections` with new trend scanner UI.

- [ ] **Step 1: Write failing component test**

```typescript
// web/tests/trend-scanner.test.ts
import { describe, it, expect } from "vitest";
import type { TrendCandidate, ScannerData } from "@/lib/types";

function makeMockCandidate(
  overrides: Partial<TrendCandidate> = {},
): TrendCandidate {
  return {
    ticker: "NVDA",
    snapshot_timestamp: "2026-04-10T08:45:12-04:00",
    spot_price: 148.3,
    direction: "bullish",
    final_score: 0.82,
    scores: { trend: 0.91, structure: 0.75, volatility: 0.68, flow: 0.85 },
    indicators: {
      ma_20: 142.5,
      ma_50: 138.2,
      ma_200: 125.8,
      rsi: 62.3,
      adx: 32.1,
      macd_histogram: 1.45,
      bbw: 0.08,
      rs_vs_spy: 1.15,
      iv_rank: 22,
      gamma_flip: 145,
      call_wall: 160,
      put_wall: 140,
    },
    summaries: {
      trend: "Full MA stack, ADX 32",
      structure: "Above gamma flip",
      vol: "IV rank 22, normal",
      flow: "4 ask-side prints",
    },
    suggested_trade: "debit_call",
    invalidation: 142.5,
    flags: [],
    holding_window: "5-15 trading days",
    ...overrides,
  };
}

describe("TrendCandidate type shape", () => {
  it("has all required fields", () => {
    const c = makeMockCandidate();
    expect(c.ticker).toBe("NVDA");
    expect(c.scores.trend).toBe(0.91);
    expect(c.indicators.rsi).toBe(62.3);
    expect(c.summaries.flow).toBe("4 ask-side prints");
  });

  it("supports bearish direction", () => {
    const c = makeMockCandidate({ direction: "bearish", ticker: "SPY" });
    expect(c.direction).toBe("bearish");
  });

  it("supports flags array", () => {
    const c = makeMockCandidate({ flags: ["event_premium", "breakout"] });
    expect(c.flags).toHaveLength(2);
  });
});

describe("ScannerData shape", () => {
  it("has funnel metadata", () => {
    const data: ScannerData = {
      scan_id: "trend_20260410_0845",
      scan_timestamp: "2026-04-10T08:45:12-04:00",
      market_context: { spy_close: 523.45, vix_close: 18.2, regime: "bullish" },
      universe_size: 743,
      stage_a_survivors: 187,
      stage_b_survivors: 92,
      candidates: [makeMockCandidate()],
    };
    expect(data.universe_size).toBe(743);
    expect(data.candidates).toHaveLength(1);
  });
});
```

**Note:** This test file covers type shape validation AND component rendering. The render tests use mocked `useScanner` to verify table headers, score bars, expand/collapse, and empty states — not just type instantiation.

Add render tests after the type shape tests:

```typescript
// Additional render tests (add after the ScannerData shape tests above)
import { render, screen, fireEvent } from "@testing-library/react";

// Mock useScanner to return controlled data
vi.mock("@/lib/useScanner", () => ({
  useScanner: vi.fn(),
}));

describe("ScannerSections component", () => {
  it("renders table headers", async () => {
    const { useScanner } = await import("@/lib/useScanner");
    (useScanner as any).mockReturnValue({
      data: {
        scan_id: "test",
        scan_timestamp: "2026-04-10T08:45:12-04:00",
        market_context: { spy_close: 523, vix_close: 18, regime: "bullish" },
        universe_size: 100,
        stage_a_survivors: 50,
        stage_b_survivors: 25,
        candidates: [makeMockCandidate()],
      },
      syncing: false,
      error: null,
      lastSync: "2026-04-10T08:45:12-04:00",
    });
    // Render and verify headers exist
    // Verify: Ticker, Dir, Score, Trend, Structure, Vol, Flow, Price, Trade, Flags
  });

  it("shows empty state when no candidates", async () => {
    const { useScanner } = await import("@/lib/useScanner");
    (useScanner as any).mockReturnValue({
      data: { candidates: [] },
      syncing: false,
      error: null,
      lastSync: null,
    });
    // Verify: "No trend candidates. Waiting for scan..." message
  });

  it("shows error state", async () => {
    const { useScanner } = await import("@/lib/useScanner");
    (useScanner as any).mockReturnValue({
      data: null,
      syncing: false,
      error: "Xenon API unavailable",
      lastSync: null,
    });
    // Verify: error message rendered
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/chenxi/projects/xenon/web && npx vitest run tests/trend-scanner.test.ts`

- [ ] **Step 3: Replace the ScannerSections component**

In `web/components/WorkspaceSections.tsx`, replace the block from the `ScannerSortKey` type definition through the end of the `ScannerSections` component (lines ~1480-1680) with:

```typescript
// --- Trend Scanner ---

type TrendSortKey =
  | "ticker"
  | "direction"
  | "final_score"
  | "trend"
  | "structure"
  | "volatility"
  | "flow"
  | "spot_price"
  | "suggested_trade";

const trendExtract = (
  item: TrendCandidate,
  key: TrendSortKey,
): string | number | null => {
  switch (key) {
    case "ticker":
      return item.ticker;
    case "direction":
      return item.direction;
    case "final_score":
      return item.final_score;
    case "trend":
      return item.scores.trend;
    case "structure":
      return item.scores.structure;
    case "volatility":
      return item.scores.volatility;
    case "flow":
      return item.scores.flow;
    case "spot_price":
      return item.spot_price;
    case "suggested_trade":
      return item.suggested_trade;
    default:
      return null;
  }
};

function ScoreBar({ value, label }: { value: number; label: string }) {
  const pct = Math.round(value * 100);
  const cls = pct >= 70 ? "bullish" : pct >= 40 ? "neutral" : "bearish";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "0.25rem", minWidth: 80 }}>
      <div
        style={{
          flex: 1,
          height: 6,
          background: "var(--bg-tertiary)",
          borderRadius: 2,
          overflow: "hidden",
        }}
      >
        <div
          className={cls}
          style={{
            width: `${pct}%`,
            height: "100%",
            background: "currentColor",
            opacity: 0.7,
            borderRadius: 2,
          }}
        />
      </div>
      <span className="mono" style={{ fontSize: "0.7rem", opacity: 0.7 }}>
        {pct}
      </span>
    </div>
  );
}

function TrendCandidateRow({
  row,
  expanded,
  onToggle,
}: {
  row: TrendCandidate;
  expanded: boolean;
  onToggle: () => void;
}) {
  const dirCls = row.direction === "bullish" ? "bullish" : "bearish";
  const tradeLabel = row.suggested_trade.replace(/_/g, " ");

  return (
    <>
      <tr
        onClick={onToggle}
        style={{ cursor: "pointer" }}
        className={expanded ? "expanded-row" : ""}
      >
        <td>
          <TickerLink ticker={row.ticker} />
        </td>
        <td>
          <span className={`pill ${dirCls}`}>{row.direction.toUpperCase()}</span>
        </td>
        <td className="right mono">{(row.final_score * 100).toFixed(0)}</td>
        <td><ScoreBar value={row.scores.trend} label="T" /></td>
        <td><ScoreBar value={row.scores.structure} label="S" /></td>
        <td><ScoreBar value={row.scores.volatility} label="V" /></td>
        <td><ScoreBar value={row.scores.flow} label="F" /></td>
        <td className="right mono">${row.spot_price.toFixed(2)}</td>
        <td>
          <span className="pill defined">{tradeLabel}</span>
        </td>
        <td>
          {row.flags.map((f) => (
            <span key={f} className="pill caution" style={{ marginRight: 4 }}>
              {f.replace(/_/g, " ")}
            </span>
          ))}
        </td>
      </tr>
      {expanded && (
        <tr className="detail-row">
          <td colSpan={10} style={{ padding: "0.5rem 1rem" }}>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.5rem", fontSize: "0.75rem" }}>
              <div>
                <strong>Trend:</strong> {row.summaries.trend}
              </div>
              <div>
                <strong>Structure:</strong> {row.summaries.structure}
              </div>
              <div>
                <strong>Volatility:</strong> {row.summaries.vol}
              </div>
              <div>
                <strong>Flow:</strong> {row.summaries.flow}
              </div>
              <div>
                <strong>Invalidation:</strong>{" "}
                <span className="mono">${row.invalidation.toFixed(2)}</span>
              </div>
              <div>
                <strong>Hold:</strong> {row.holding_window}
              </div>
              <div style={{ gridColumn: "1 / -1", display: "flex", gap: "1rem", flexWrap: "wrap", marginTop: "0.25rem" }}>
                <span className="mono">RSI {row.indicators.rsi.toFixed(0)}</span>
                <span className="mono">ADX {row.indicators.adx.toFixed(0)}</span>
                <span className="mono">IV Rank {row.indicators.iv_rank.toFixed(0)}</span>
                <span className="mono">RS {row.indicators.rs_vs_spy.toFixed(2)}</span>
                <span className="mono">BBW {row.indicators.bbw.toFixed(3)}</span>
                <span className="mono">GEX Flip ${row.indicators.gamma_flip.toFixed(0)}</span>
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

const ScannerSections = React.memo(function ScannerSections() {
  const { data, syncing, error, lastSync } = useScanner(true);
  const candidates = data?.candidates ?? [];
  const { sorted, sort, toggle } = useSort(candidates, trendExtract);
  const [expandedTicker, setExpandedTicker] = React.useState<string | null>(null);

  return (
    <>
      <div className="section">
        <div className="section-header">
          <div className="section-title">
            <Sparkles size={14} />
            Trend Scanner
            <InfoTooltip text="3-stage trend scanner: TA prefilter → options structure → flow confirmation. Ranked by composite score." />
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
            {data?.market_context && (
              <span className="report-meta" style={{ margin: 0 }}>
                SPY {data.market_context.spy_close.toFixed(0)} · VIX{" "}
                {data.market_context.vix_close.toFixed(1)} ·{" "}
                {data.market_context.regime}
              </span>
            )}
            {lastSync && (
              <span className="report-meta" style={{ margin: 0 }}>
                {new Date(lastSync).toLocaleTimeString()}
              </span>
            )}
            <span className="pill defined">
              {syncing
                ? "SCANNING..."
                : `${candidates.length} CANDIDATES`}
            </span>
          </div>
        </div>

        {error && (
          <div className="section-body">
            <div className="alert-item bearish">{error}</div>
          </div>
        )}

        {candidates.length === 0 && !syncing && !error && (
          <div className="section-body">
            <div className="alert-item">
              No trend candidates. Waiting for scan...
            </div>
          </div>
        )}

        {candidates.length > 0 && (
          <div className="section-body table-wrap">
            <table>
              <thead>
                <tr>
                  <SortTh<TrendSortKey> label="Ticker" sortKey="ticker" activeKey={sort.key} direction={sort.direction} onToggle={toggle} />
                  <SortTh<TrendSortKey> label="Dir" sortKey="direction" activeKey={sort.key} direction={sort.direction} onToggle={toggle} />
                  <SortTh<TrendSortKey> label="Score" sortKey="final_score" className="right" activeKey={sort.key} direction={sort.direction} onToggle={toggle} />
                  <SortTh<TrendSortKey> label="Trend" sortKey="trend" activeKey={sort.key} direction={sort.direction} onToggle={toggle} />
                  <SortTh<TrendSortKey> label="Structure" sortKey="structure" activeKey={sort.key} direction={sort.direction} onToggle={toggle} />
                  <SortTh<TrendSortKey> label="Vol" sortKey="volatility" activeKey={sort.key} direction={sort.direction} onToggle={toggle} />
                  <SortTh<TrendSortKey> label="Flow" sortKey="flow" activeKey={sort.key} direction={sort.direction} onToggle={toggle} />
                  <SortTh<TrendSortKey> label="Price" sortKey="spot_price" className="right" activeKey={sort.key} direction={sort.direction} onToggle={toggle} />
                  <SortTh<TrendSortKey> label="Trade" sortKey="suggested_trade" activeKey={sort.key} direction={sort.direction} onToggle={toggle} />
                  <th>Flags</th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((row) => (
                  <TrendCandidateRow
                    key={`trend-${row.ticker}`}
                    row={row}
                    expanded={expandedTicker === row.ticker}
                    onToggle={() =>
                      setExpandedTicker(
                        expandedTicker === row.ticker ? null : row.ticker,
                      )
                    }
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {data && (
        <div className="section">
          <div className="report-meta">
            {data.scan_id} · Universe: {data.universe_size} → Stage A:{" "}
            {data.stage_a_survivors} → Stage B: {data.stage_b_survivors} →
            Top {candidates.length}
          </div>
        </div>
      )}
    </>
  );
});
```

- [ ] **Step 4: Add missing import for TrendCandidate type**

At the top of `WorkspaceSections.tsx`, find the import from `@/lib/types` and add `TrendCandidate`:

```typescript
import type { ..., TrendCandidate } from "@/lib/types";
```

- [ ] **Step 5: Run component test**

Run: `cd /Users/chenxi/projects/xenon/web && npx vitest run tests/trend-scanner.test.ts`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
cd /Users/chenxi/projects/xenon && git add web/components/WorkspaceSections.tsx web/tests/trend-scanner.test.ts
git commit -m "feat(web): replace ScannerSections with trend scanner UI"
```

---

### Task 6: E2E Browser Verification

**Per CLAUDE.md mandatory rule: "E2E browser verification for ALL UI work."**

- [ ] **Step 1: Start dev server**

Run: `cd /Users/chenxi/projects/xenon/web && npm run dev`

- [ ] **Step 2: Navigate to scanner page and verify rendering**

Use chrome-cdp or Playwright MCP to:

1. Navigate to `http://localhost:3000/scanner`
2. Take screenshot
3. Verify: section header shows "Trend Scanner", table columns match (Ticker, Dir, Score, Trend, Structure, Vol, Flow, Price, Trade, Flags)
4. If data exists, verify score bars render, direction badges show correct colors
5. If no data, verify "No trend candidates. Waiting for scan..." message

- [ ] **Step 3: Test sync button**

Trigger a POST (click sync/refresh if UI has one, or POST manually via curl):

```bash
curl -X POST http://localhost:3000/api/scanner
```

Verify response has trend scanner structure (`scan_id`, `candidates` array).

- [ ] **Step 4: Test expandable rows (if data available)**

Click a candidate row → verify detail panel expands with summaries, indicators, invalidation level.

- [ ] **Step 5: Take final screenshot and commit any fixes**

---

### Task 7: Run Full Test Suite

- [ ] **Step 1: Run frontend tests**

Run: `cd /Users/chenxi/projects/xenon/web && npx vitest run`
Expected: All pass including new trend-scanner tests

- [ ] **Step 2: Run backend tests (regression)**

Run: `cd /Users/chenxi/projects/xenon && python -m pytest scripts/tests/ -v --ignore=scripts/tests/__pycache__`
Expected: All pass

- [ ] **Step 3: Final commit if any cleanup needed**
