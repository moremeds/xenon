# Portfolio "By Structure" View — Design

**Date:** 2026-04-07
**Status:** Approved — ready for implementation plan
**Scope:** Frontend only (`web/`). No API, schema, or Python changes.

## Summary

Add a second grouping mode to the Portfolio positions section. Toggle between **By Risk** (existing: defined / undefined / equity) and **By Structure** (new, **default**). The Structure view groups positions by underlying ticker, then sub-groups each ticker's options by the `category` field from `docs/trading/options-structures.json`, with the stock row pinned on top of each ticker card.

## Motivation

Current view (`PortfolioSections` in `web/components/WorkspaceSections.tsx:921-1036`) splits positions into three sibling sections by `risk_profile`. This is great for naked-short audits but obscures per-underlying state: a trader holding TSLA stock + a bull call spread + a covered call has to scan three sections to reconstruct their TSLA exposure.

A "By Structure" view grouped by underlying answers: *"what is my complete position in TSLA, broken down by structure family?"* — which is the natural unit for sizing, delta management, and trade planning.

## Non-goals

- No server-side category classification (stays frontend).
- No cross-ticker roll-ups ("total verticals across portfolio").
- No drag-to-reorder or user-customizable sort.
- Toggle persistence is localStorage only — not synced across devices.

## Decisions (from brainstorming)

| # | Decision |
|---|----------|
| 1 | Toggle at Portfolio section header: `[ By Risk ] [ By Structure ]`. Persisted in `localStorage["xenon.portfolio.view"]`. **Default = "structure"** on first visit. |
| 2 | Ticker cards ordered by `\|Σ market value\|` desc. Inside each card: stock row first, then categories in catalog order (`single, vertical, covered, collar, straddle, strangle, butterfly, condor, ratio, synthetic, horizontal, complex, other`). |
| 3 | Tickers with only stock still get their own card ("one ticker = one card" rule). |
| 4 | Card header shows: ticker, last, day%, Σ MV, Σ Day P&L, Σ Total P&L (% in parens), net Δ chip. |
| 5 | Category mapping is a static frontend map built at module-load from `options-structures.json` (keyed by `name` + every `aliases` entry, lowercased). Lookup key on each position is **`structure_type`** (the normalized string, e.g. `"Short Put"`), NOT `structure` (which is decorated: `"Short Put $440.0"`). Unknown strings fall into `"other"`. |
| 6 | Category sub-groups inside a card are collapsible; **all expanded by default**. Collapse state is ephemeral React state (not persisted). |

## Architecture

### Files touched

| File | Change |
|------|--------|
| `web/lib/structureCatalog.ts` *(new)* | Loads `docs/trading/options-structures.json` via a static TS import. Exports `getStructureCategory(structure: string): CategoryKey`, `CATEGORY_ORDER`, `CATEGORY_LABELS`, `CategoryKey` type. |
| `web/components/PortfolioByStructure.tsx` *(new)* | The new view component. Takes the same `{ portfolio, prices, activeAccount }` props as today's inner render. |
| `web/components/WorkspaceSections.tsx` | `PortfolioSections` gains a `viewMode` state (`"structure" \| "risk"`, default `"structure"`, persisted). Renders toggle pill in a new header row, then branches to existing risk sections or `<PortfolioByStructure>`. |
| `web/components/PositionTable.tsx` | **Unchanged.** Reused as-is for each category's rows. |
| `web/tests/portfolio-by-structure.test.tsx` *(new)* | Vitest unit tests for grouping, ordering, category mapping, collapse state. |
| `web/tests/e2e/portfolio-view-toggle.spec.ts` *(new)* | Playwright E2E test for the toggle + default view + persistence. |

### `structureCatalog.ts`

**Primary key = `structure_type`.** Real IB rows produce `structure = "Short Put $440.0"` but `structure_type = "Short Put"` (verified against `data/portfolio.json`). The catalog must be queried with `structure_type`. Fallback to `structure.replace(/\s*\$[\d.]+.*$/, "").trim()` only if `structure_type` is absent.

```ts
export type CategoryKey =
  | "single" | "vertical" | "covered" | "collar"
  | "straddle" | "strangle" | "butterfly" | "condor"
  | "ratio" | "synthetic" | "horizontal" | "complex" | "other";

export const CATEGORY_ORDER: readonly CategoryKey[] = [
  "single","vertical","covered","collar","straddle","strangle",
  "butterfly","condor","ratio","synthetic","horizontal","complex","other",
];

export const CATEGORY_LABELS: Record<CategoryKey, string> = {
  single: "Single", vertical: "Vertical", covered: "Covered", collar: "Collar",
  straddle: "Straddle", strangle: "Strangle", butterfly: "Butterfly",
  condor: "Condor", ratio: "Ratio", synthetic: "Synthetic",
  horizontal: "Horizontal", complex: "Complex", other: "Other",
};

/** Returns the catalog category for a structure string. Unknown → "other". */
export function getStructureCategory(structure: string): CategoryKey;
```

- **Construction:** at module load, iterate the imported JSON once, building `Map<string, CategoryKey>`. For each entry, index `entry.name` and every `entry.aliases[i]`, normalized with `.trim().toLowerCase()`.
- **Lookup:** `getStructureCategory(s)` normalizes the input the same way and returns the mapped category or `"other"`.
- **Dev-time miss warning:** maintain a `Set<string>` of misses; `console.warn` once per unique key when `process.env.NODE_ENV !== "production"`. Does not affect runtime behavior.
- **Stock rows are detected via `structure_type === "Stock"`** (the authoritative discriminator used in `PositionTable.tsx:139`, `positionUtils.ts:83`, `WorkspaceShell.tsx:79`), **not** via `risk_profile === "equity"`. Stock rows never reach `getStructureCategory`.

### `PortfolioByStructure.tsx`

**Data shape built from the flat positions array:**

```ts
type TickerGroup = {
  ticker: string;
  stock: PortfolioPosition | null;
  optionsByCategory: Map<CategoryKey, PortfolioPosition[]>;
  agg: {
    mv: number | null;          // Σ resolveMarketValue — null if ALL contributors null
    entryCost: number;          // Σ resolveEntryCost
    dayPnl: number | null;      // Σ getTodayPnlDollars — null if ALL contributors null
    totalPnl: number | null;    // mv − entryCost, null if mv null
    totalPnlPct: number | null; // totalPnl / |entryCost| — null if entryCost === 0 or totalPnl null
    netDelta: number | null;    // Σ positionDeltaDetailed(…).signed — null if all contributors null
  };
  last: number | null;
  dayChgPct: number | null;
};
```

**Helper reuse — use these exact functions, do not reimplement:**
- `resolveMarketValue(pos)` — `web/lib/positionUtils.ts:68`
- `resolveEntryCost(pos)` — `web/lib/positionUtils.ts:87`
- `getTodayPnlDollars(pos, prices)` — `web/lib/positionUtils.ts:289`
- Signed delta — extract from the existing `positionDeltaDetailed` in `web/lib/exposureBreakdown.ts:61`. If that helper is not currently exported in a reusable form, export it (or a thin wrapper returning `{ signed }`) as part of this change. **Do not write a second delta implementation.**

**Build pipeline:**
1. Bucket positions by `ticker`.
2. Within each bucket: separate `structure_type === "Stock"` as `stock`; classify the rest via `getStructureCategory(p.structure_type)`.
3. Aggregate header metrics using the helpers above. Nulls propagate (any contributor null → skipped in sum; if every contributor is null, the aggregate is null).
4. `totalPnlPct` is `null` when `entryCost === 0` (mirrors the row-level guard at `PositionTable.tsx:272`); header renders `—`.
5. Sort ticker groups by `|agg.mv ?? 0|` desc. Stable sort (ties preserve insertion order).
6. Inside a group, iterate `CATEGORY_ORDER`, skipping empty categories.

**Rendering:**
- Outer: one `<div className="section">` per ticker group — the same `section` primitive the current view uses.
- Section header = ticker card header: ticker + last + day% + MV + day P&L + total P&L (% in parens) + `Δ` chip.
- Body: if `stock` is present, render a single-row `<PositionTable positions={[stock]} showExpiry={false} prices={prices} readonly={…} />` at the top, then per-category sub-blocks.
- Each sub-block: thin rule header with uppercase mono label + count pill on right + chevron button (collapse toggle). Body: `<PositionTable positions={rowsInCategory} showUnderlying={true} …/>`.
- Collapsed state stored as `Record<string, boolean>` in local React state, keyed by `${activeAccount}:${ticker}:${category}` so that IB and FUTU collapse state do not bleed across account switches. Keyboard-accessible: chevron is a `<button aria-expanded>`. Additionally, the parent `PortfolioSections` should be `key={activeAccount}`-ed (or the collapse state explicitly reset on account change via `useEffect`) to guarantee a clean slate — pick whichever is cheaper given the surrounding code and note it in the plan.

**Filter/search parity with existing view.** Today's risk view wires `useTableFilter` into each of the three sections (`WorkspaceSections.tsx:931-933`) and a regression test asserts the search inputs exist (`web/tests/workspace-sections-table-search-headers.test.ts`). The Structure view must preserve a search experience OR the test must be updated in the same PR. This spec chooses: **one portfolio-level search box** lives next to the toggle; the query filters the flat positions list before grouping, so both views stay in sync, and the regression test is updated to assert a single portfolio-level search input in Structure mode + three section searches in Risk mode. `extractPositionSearchText` already exists at `WorkspaceSections.tsx:927` — reuse it.

### Toggle in `WorkspaceSections.tsx`

Inside `PortfolioSections`, before any content section, render a new header row containing the Portfolio title + segmented toggle. Check for an existing `SegmentedToggle` component (the IB/Futu account switcher uses this pattern); if present, reuse it. Otherwise add inline markup with the same class names as the account switcher.

```tsx
const [viewMode, setViewMode] = useState<"structure" | "risk">("structure");

useEffect(() => {
  const stored = typeof window !== "undefined"
    ? window.localStorage.getItem("xenon.portfolio.view")
    : null;
  if (stored === "risk" || stored === "structure") setViewMode(stored);
}, []);

const updateMode = (m: "structure" | "risk") => {
  setViewMode(m);
  try { window.localStorage.setItem("xenon.portfolio.view", m); } catch {}
};
```

- SSR-safe: initial render uses `viewMode = null` (unknown), the `useEffect` hydrates from localStorage, and the portfolio section renders a neutral loading shell (`<div className="section"><div className="alert-item">Loading portfolio view…</div></div>`) until `viewMode` is resolved. This avoids painting the Structure tree and then swapping to Risk (the two trees differ substantially — Risk has three filtered sections at `WorkspaceSections.tsx:953/980/1005`).

## Data Flow

```
portfolio.positions (flat)
      │
      ▼
groupByTicker → for each ticker:
      ├── stock (risk_profile === "equity")
      └── options → getStructureCategory(p.structure) → bucket
      │
      ▼
aggregate header metrics per ticker (reusing existing helpers)
      │
      ▼
sort tickers by |MV| desc
      │
      ▼
render one `.section` per ticker → stock row → PositionTable per non-empty category
```

WS price updates flow through the existing `prices` prop — no new subscription, same flash animations, same per-leg P&L math already validated in `daily-chg.test.ts` and `order-reliability.test.ts`.

## Correctness Invariants (from `web/CLAUDE.md`)

- **Credit/debit sign preserved** in all aggregations — never `Math.abs()` on prices.
- **Day Chg %** uses `getOptionDailyChg()`, denominator = yesterday's close value, never entry cost.
- **Total P&L %** = `(MV − EC) / |EC| × 100`.
- **Brand:** 4px panel radius, 999px pill capsule, token colors only, mono for machine / sans for product, no decorative elements.

## Edge Cases

| Case | Handling |
|------|----------|
| Ticker has only stock | Card shows stock row only, no category sub-headers |
| Ticker has only options | No stock row; card header MV/Δ from options alone |
| `structure` string not in catalog | Falls into `"other"` category (last in order); dev console warns once per unique miss |
| Two positions with same structure at different expiries | Both listed as sibling rows inside the same sub-block |
| `portfolio` is null | Reuse existing loading card; toggle still renders but disabled |
| Zero positions | Empty state with just the toggle header |
| localStorage unavailable / SSR | Falls back to default `"structure"` on client |
| Case-inconsistent `structure` (`"long call"` vs `"Long Call"`) | Normalized at lookup and at catalog-index time |
| `market_value` null for a row | Excluded from `Σ mv` sum (not treated as zero). Aggregate mv is null iff every contributor is null. Header renders `—` for null aggregates. |
| `entry_cost` is 0 for the whole ticker | `totalPnlPct` is null → header renders `—`. Matches row-level guard at `PositionTable.tsx:272`. |
| `day P&L` null (missing close or live price) | Excluded from sum; aggregate null iff all contributors null. Never flattened to zero — unknown stays unknown. |
| delta null for a row | Excluded from `Σ delta` sum; Δ chip renders `—` when aggregate is null. |
| Account switch (IB ↔ Futu) | `readonly` prop propagates to every PositionTable instance, same as today. Collapse state is keyed by `activeAccount` so it does not bleed across accounts. View mode (`risk` vs `structure`) is a global user preference and is NOT reset on account switch. |

## Testing

### Unit — `web/tests/portfolio-by-structure.test.tsx`

1. `getStructureCategory("Bull Call Spread") === "vertical"`
2. Alias lookup: `getStructureCategory("Protective Put") === "single"` (Long Put alias)
3. Case insensitivity: `getStructureCategory("  long call  ") === "single"`
4. Unknown → `"other"`; dev warn fires once, not twice for repeat lookup of same key
5. Grouping: fixture with 1 stock + 2 verticals + 1 covered for TSLA produces one `TickerGroup` with correct `stock`, `optionsByCategory.get("vertical").length === 2`, `optionsByCategory.get("covered").length === 1`
6. Ordering: three tickers with MV `50k / 200k / 10k` render in order `200k, 50k, 10k`
7. Stock-only ticker renders with no category sub-blocks
8. Empty categories are skipped in render
9. Collapse state toggles per `(ticker, category)` key, doesn't affect siblings
10. Header aggregation: hand-crafted fixture with known MV / EC / day P&L produces expected header numbers (regression guard against sign errors)

### E2E — `web/tests/e2e/portfolio-view-toggle.spec.ts`

1. `/portfolio` loads with Structure view active by default (segmented toggle shows `By Structure` highlighted)
2. First ticker card corresponds to the largest-|MV| ticker in the fixture
3. Click `By Risk` → existing Defined/Undefined/Equity sections appear
4. Reload → stays on `By Risk` (localStorage round-trip)
5. Click `By Structure` → returns to ticker cards
6. Collapse a category header → rows hide; expand → rows return; aria-expanded toggles
7. Visual snapshot of a card with stock + two categories
8. Account-switch safety: load Structure view, collapse `TSLA:vertical`, switch IB→FUTU, confirm FUTU portfolio renders independently with fresh collapse state and read-only rows, switch back to IB, confirm the original collapse state is either restored (if keyed) or reset (if cleared) — whichever the implementation settles on, assert it explicitly.
9. Real-data category mapping: fixture mirrors `data/portfolio.json` shape (`structure = "Short Put $440.0"`, `structure_type = "Short Put"`) and renders under `SINGLE` category, not `OTHER`. Guards against ISSUE-1 regression.

### Coverage target

95% for new files (`structureCatalog.ts`, `PortfolioByStructure.tsx`) per project policy.

## Open Questions

None — all decisions locked during brainstorming.

## Review trail

Spec reviewed via `/codex-review` tribunal on 2026-04-07. Codex (gpt-5.3-codex) raised 10 issues, all accepted after Claude verification against the live codebase. Gemini run returned empty output — degraded to bilateral (Codex + Claude). Fixes applied inline before this version was committed:

| Codex Issue | Fix |
|-------------|-----|
| ISSUE-1 `structure` vs `structure_type` | Catalog lookup now keyed off `structure_type` (verified against `data/portfolio.json`). Unit test 9 added. |
| ISSUE-2 wrong helper surface | Explicit reuse of `resolveMarketValue` / `resolveEntryCost` / `getTodayPnlDollars` from `positionUtils.ts`, by file:line. |
| ISSUE-3 `useTableFilter` parity | Portfolio-level search added next to toggle; regression test `workspace-sections-table-search-headers.test.ts` updated in same PR. |
| ISSUE-4 collapse bleed across accounts | Collapse keys namespaced `${activeAccount}:${ticker}:${category}`; E2E test 8 added. |
| ISSUE-5 stock detection via `risk_profile` | Switched to `structure_type === "Stock"` (matches `PositionTable.tsx:139`). |
| ISSUE-6 divide-by-zero `totalPnlPct` | Null when `entryCost === 0`; mirrors row-level guard. |
| ISSUE-7 second delta implementation | Reuse `positionDeltaDetailed` from `exposureBreakdown.ts:61`; export if needed. |
| ISSUE-8 null handling for day metrics | Explicit null semantics in Edge Cases table — unknown stays unknown. |
| ISSUE-9 missing account-switch E2E | E2E test 8 added. |
| ISSUE-10 hydration flash rationale | Loading shell until `viewMode` hydrated; no DOM swap. |

