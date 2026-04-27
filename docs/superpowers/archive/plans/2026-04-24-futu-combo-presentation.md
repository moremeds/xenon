# Futu Combo Presentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Futu portfolio tab render detected option spreads as a single collapsible combo row (with DEBIT/CREDIT badge, net entry, net MV, net P&L) — visually matching how the IB tab renders BAG combos today.

**Architecture:** Add a pure fusion helper `fuseVirtualPair()` in `web/lib/portfolioByStructure.ts` that takes two paired single-leg `PortfolioPosition`s and returns one multi-leg `PortfolioPosition`. Gate it behind a new opt-in `opts.fuseVirtualPairs` on `buildTickerGroups()`. The Futu caller opts in; IB does not — zero regression surface for IB order flows. `PositionTable` already renders multi-leg positions as collapsible combo rows, so no renderer changes beyond a small label-suppression fix.

**Tech Stack:** TypeScript, React (Next.js App Router), Vitest (unit + component), Playwright (E2E). Existing helpers: `sumOrNull()`, `resolveStructureKey()`, `getStructureCategory()`.

**Spec:** `docs/superpowers/specs/2026-04-24-futu-combo-presentation-design.md`

---

## File Structure

| File                                                   | Role                                      | State  |
| ------------------------------------------------------ | ----------------------------------------- | ------ |
| `web/lib/portfolioByStructure.ts`                      | Grouping + new fusion logic               | modify |
| `web/tests/fuse-virtual-pair.test.ts`                  | Unit tests for `fuseVirtualPair()`        | create |
| `web/tests/portfolio-by-structure.test.ts`             | Extend with `fuseVirtualPairs: true` path | modify |
| `web/components/PortfolioByStructure.tsx`              | Opt-in for Futu; suppress redundant label | modify |
| `web/tests/portfolio-by-structure-futu-combo.test.tsx` | Component-level combo rendering test      | create |
| `web/e2e/futu-combo-presentation.spec.ts`              | E2E: combo row visible on Futu tab        | create |

---

### Task 1: Add `fuseVirtualPair()` pure helper (TDD)

**Files:**

- Create: `web/tests/fuse-virtual-pair.test.ts`
- Modify: `web/lib/portfolioByStructure.ts` (export new `fuseVirtualPair`)

- [ ] **Step 1: Write failing tests for `fuseVirtualPair()`**

Create `web/tests/fuse-virtual-pair.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import { fuseVirtualPair } from "@/lib/portfolioByStructure";
import type { PortfolioPosition, PortfolioLeg } from "@/lib/types";

function mkLeg(overrides: Partial<PortfolioLeg> = {}): PortfolioLeg {
  return {
    direction: "LONG",
    contracts: 10,
    type: "Put",
    strike: 400,
    entry_cost: 4000,
    avg_cost: 40,
    market_price: 38,
    market_value: 3800,
    market_price_is_calculated: false,
    ...overrides,
  };
}

function mkPos(
  id: number,
  leg: PortfolioLeg,
  overrides: Partial<PortfolioPosition> = {},
): PortfolioPosition {
  return {
    id,
    ticker: "TSLA",
    structure: `${leg.direction === "LONG" ? "Long" : "Short"} ${leg.type}`,
    structure_type: `${leg.direction === "LONG" ? "Long" : "Short"} ${leg.type}`,
    risk_profile: leg.direction === "LONG" ? "limited_risk" : "unlimited_risk",
    expiry: "2027-01-15",
    contracts: leg.contracts,
    direction: leg.direction,
    entry_cost: leg.entry_cost,
    max_risk: null,
    market_value: leg.market_value,
    legs: [leg],
    ib_daily_pnl: null,
    kelly_optimal: null,
    target: null,
    stop: null,
    entry_date: "2026-01-01",
    ...overrides,
  };
}

describe("fuseVirtualPair", () => {
  const pair = {
    pairKey: "vp-1",
    label: "Bull Put Spread $390/$400 · 2027-01-15",
  };

  it("fuses a Bull Put Spread (Short $390 / Long $400) as CREDIT", () => {
    const shortLeg = mkPos(
      1,
      mkLeg({
        direction: "SHORT",
        type: "Put",
        strike: 390,
        contracts: 10,
        entry_cost: -7000,
        market_value: -6800,
      }),
    );
    const longLeg = mkPos(
      2,
      mkLeg({
        direction: "LONG",
        type: "Put",
        strike: 400,
        contracts: 10,
        entry_cost: 4000,
        market_value: 3800,
      }),
    );

    const fused = fuseVirtualPair(shortLeg, longLeg, pair, 0);

    expect(fused.id).toBeLessThan(0);
    expect(fused.ticker).toBe("TSLA");
    expect(fused.expiry).toBe("2027-01-15");
    expect(fused.contracts).toBe(10);
    expect(fused.structure_type).toBe("Bull Put Spread");
    expect(fused.direction).toBe("CREDIT"); // entry_cost = -7000 + 4000 = -3000
    expect(fused.entry_cost).toBe(-3000);
    expect(fused.market_value).toBe(-3000); // -6800 + 3800
    expect(fused.legs).toHaveLength(2);
    expect(fused.legs[0].direction).toBe("LONG"); // LONG first for verticals
    expect(fused.legs[1].direction).toBe("SHORT");
  });

  it("fuses a Bear Put Spread (Long $400 / Short $390) as DEBIT", () => {
    const longLeg = mkPos(
      3,
      mkLeg({
        direction: "LONG",
        type: "Put",
        strike: 400,
        contracts: 5,
        entry_cost: 2000,
        market_value: 1800,
      }),
    );
    const shortLeg = mkPos(
      4,
      mkLeg({
        direction: "SHORT",
        type: "Put",
        strike: 390,
        contracts: 5,
        entry_cost: -500,
        market_value: -400,
      }),
    );

    const fused = fuseVirtualPair(
      longLeg,
      shortLeg,
      { pairKey: "vp-2", label: "Bear Put Spread $390/$400 · 2027-01-15" },
      1,
    );

    expect(fused.structure_type).toBe("Bear Put Spread");
    expect(fused.direction).toBe("DEBIT"); // 2000 + (-500) = 1500
    expect(fused.entry_cost).toBe(1500);
    expect(fused.market_value).toBe(1400);
  });

  it("fuses a Long Straddle in strike-ascending leg order", () => {
    const callLeg = mkPos(
      5,
      mkLeg({
        direction: "LONG",
        type: "Call",
        strike: 400,
        contracts: 3,
        entry_cost: 900,
        market_value: 1200,
      }),
    );
    const putLeg = mkPos(
      6,
      mkLeg({
        direction: "LONG",
        type: "Put",
        strike: 400,
        contracts: 3,
        entry_cost: 600,
        market_value: 500,
      }),
    );

    const fused = fuseVirtualPair(
      callLeg,
      putLeg,
      { pairKey: "vp-3", label: "Long Straddle $400 · 2027-01-15" },
      2,
    );

    expect(fused.structure_type).toBe("Long Straddle");
    expect(fused.direction).toBe("DEBIT");
    expect(fused.legs.map((l) => l.strike)).toEqual([400, 400]);
  });

  it("propagates null market_value via sumOrNull", () => {
    const a = mkPos(
      7,
      mkLeg({
        direction: "LONG",
        type: "Put",
        strike: 400,
        entry_cost: 1000,
        market_value: null,
      }),
    );
    const b = mkPos(
      8,
      mkLeg({
        direction: "SHORT",
        type: "Put",
        strike: 390,
        entry_cost: -500,
        market_value: -200,
      }),
    );

    const fused = fuseVirtualPair(
      a,
      b,
      { pairKey: "vp-4", label: "Bear Put Spread $390/$400 · 2027-01-15" },
      3,
    );

    // one null + one non-null → non-null sum (sumOrNull skips nulls, yields non-null iff any value)
    expect(fused.market_value).toBe(-200);
  });

  it("returns null market_value when both legs are null", () => {
    const a = mkPos(
      9,
      mkLeg({
        direction: "LONG",
        type: "Put",
        strike: 400,
        market_value: null,
      }),
    );
    const b = mkPos(
      10,
      mkLeg({
        direction: "SHORT",
        type: "Put",
        strike: 390,
        market_value: null,
      }),
    );

    const fused = fuseVirtualPair(
      a,
      b,
      { pairKey: "vp-5", label: "Bear Put Spread $390/$400 · 2027-01-15" },
      4,
    );

    expect(fused.market_value).toBeNull();
  });

  it("picks earliest non-empty entry_date; empty string when both empty", () => {
    const a = mkPos(
      11,
      mkLeg({ direction: "LONG", type: "Put", strike: 400 }),
      { entry_date: "" },
    );
    const b = mkPos(
      12,
      mkLeg({ direction: "SHORT", type: "Put", strike: 390 }),
      { entry_date: "2026-03-10" },
    );

    const fused = fuseVirtualPair(
      a,
      b,
      { pairKey: "vp-6", label: "Bear Put Spread $390/$400 · 2027-01-15" },
      5,
    );
    expect(fused.entry_date).toBe("2026-03-10");

    const c = mkPos(
      13,
      mkLeg({ direction: "LONG", type: "Put", strike: 400 }),
      { entry_date: "" },
    );
    const d = mkPos(
      14,
      mkLeg({ direction: "SHORT", type: "Put", strike: 390 }),
      { entry_date: "" },
    );

    const fused2 = fuseVirtualPair(
      c,
      d,
      { pairKey: "vp-7", label: "Bear Put Spread $390/$400 · 2027-01-15" },
      6,
    );
    expect(fused2.entry_date).toBe("");
  });

  it("synthetic ids from different syntheticIdSeq values do not collide", () => {
    const a = mkPos(15, mkLeg({ direction: "LONG", type: "Put", strike: 400 }));
    const b = mkPos(
      16,
      mkLeg({ direction: "SHORT", type: "Put", strike: 390 }),
    );
    const f1 = fuseVirtualPair(a, b, { pairKey: "vp-8", label: "x" }, 0);
    const f2 = fuseVirtualPair(a, b, { pairKey: "vp-9", label: "y" }, 1);
    expect(f1.id).not.toBe(f2.id);
    expect(f1.id).toBeLessThan(0);
    expect(f2.id).toBeLessThan(0);
  });
});
```

- [ ] **Step 2: Run tests — expect failure**

Run: `cd web && npm test -- fuse-virtual-pair`
Expected: FAIL — `fuseVirtualPair is not exported from @/lib/portfolioByStructure`

- [ ] **Step 3: Implement `fuseVirtualPair()`**

In `web/lib/portfolioByStructure.ts`, add after the `sumOrNull` function (before `buildTickerGroups`):

```ts
/**
 * Structure-type derivation from a virtual pair's legs. Returns the
 * canonical catalog name so downstream consumers (label, risk_profile
 * lookup) can round-trip through `structureCatalog`.
 */
function deriveFusedStructureType(a: PortfolioLeg, b: PortfolioLeg): string {
  const longLeg = a.direction === "LONG" ? a : b;
  const shortLeg = a.direction === "SHORT" ? a : b;
  const sameType = a.type === b.type;
  const sameDir = a.direction === b.direction;

  if (sameType && !sameDir && (a.type === "Put" || a.type === "Call")) {
    // Vertical
    const ls = longLeg.strike ?? 0;
    const ss = shortLeg.strike ?? 0;
    if (a.type === "Put")
      return ls < ss ? "Bull Put Spread" : "Bear Put Spread";
    return ls < ss ? "Bull Call Spread" : "Bear Call Spread";
  }
  if (!sameType && sameDir) {
    // Straddle (same strike) or Strangle (different strike)
    const sameStrike = a.strike === b.strike;
    const prefix = a.direction === "LONG" ? "Long" : "Short";
    return sameStrike ? `${prefix} Straddle` : `${prefix} Strangle`;
  }
  if (!sameType && !sameDir) {
    // Synthetic / Risk Reversal
    const sameStrike = a.strike === b.strike;
    return sameStrike ? "Synthetic" : "Risk Reversal";
  }
  return "Complex";
}

function orderFusedLegs(
  a: PortfolioLeg,
  b: PortfolioLeg,
): [PortfolioLeg, PortfolioLeg] {
  // Verticals / synthetics: LONG before SHORT.
  if (a.direction !== b.direction) {
    return a.direction === "LONG" ? [a, b] : [b, a];
  }
  // Straddle / strangle (same direction): sort by strike ascending.
  const as = a.strike ?? 0;
  const bs = b.strike ?? 0;
  return as <= bs ? [a, b] : [b, a];
}

/**
 * Synthesize a multi-leg PortfolioPosition from a detected virtual pair.
 * Caller guarantees: same ticker, same expiry, 1 leg each, equal contracts.
 *
 * Sign convention (verified in futuPortfolioAdapter.ts): SHORT legs carry
 * negative entry_cost and negative market_value. Simple summation yields
 * correct net DEBIT/CREDIT sign with no abs() calls.
 */
export function fuseVirtualPair(
  a: PortfolioPosition,
  b: PortfolioPosition,
  pair: VirtualPair,
  syntheticIdSeq: number,
): PortfolioPosition {
  const [legA, legB] = orderFusedLegs(a.legs[0], b.legs[0]);
  const structureType = deriveFusedStructureType(a.legs[0], b.legs[0]);
  const lo = Math.min(legA.strike ?? 0, legB.strike ?? 0);
  const hi = Math.max(legA.strike ?? 0, legB.strike ?? 0);
  const structure = `${a.ticker} ${structureType} $${lo}/$${hi}`;

  const entryCost = a.entry_cost + b.entry_cost;
  const marketValue = sumOrNull([a.market_value, b.market_value]);
  const ibDailyPnl = sumOrNull([
    a.ib_daily_pnl ?? null,
    b.ib_daily_pnl ?? null,
  ]);

  let direction: "DEBIT" | "CREDIT" | "FLAT";
  if (entryCost > 0) direction = "DEBIT";
  else if (entryCost < 0) direction = "CREDIT";
  else direction = "FLAT";

  const dates = [a.entry_date, b.entry_date]
    .filter((s) => s && s.length > 0)
    .sort();
  const entryDate = dates[0] ?? "";

  return {
    id: -(1_000_000 + syntheticIdSeq),
    ticker: a.ticker,
    structure,
    structure_type: structureType,
    risk_profile: a.risk_profile || b.risk_profile || "",
    expiry: a.expiry,
    contracts: a.legs[0].contracts,
    direction,
    entry_cost: entryCost,
    max_risk: null,
    market_value: marketValue,
    legs: [legA, legB],
    market_price_is_calculated:
      a.market_price_is_calculated === true ||
      b.market_price_is_calculated === true,
    ib_daily_pnl: ibDailyPnl,
    kelly_optimal: null,
    target: null,
    stop: null,
    entry_date: entryDate,
  };
}
```

Also ensure `PortfolioLeg` is imported at the top of the file:

```ts
import type { PortfolioPosition, PortfolioLeg } from "@/lib/types";
```

(The existing import only pulls `PortfolioPosition` — extend it.)

- [ ] **Step 4: Run tests — expect pass**

Run: `cd web && npm test -- fuse-virtual-pair`
Expected: 7 tests pass.

- [ ] **Step 5: Commit**

```bash
git add web/lib/portfolioByStructure.ts web/tests/fuse-virtual-pair.test.ts
git commit -m "feat(portfolio): fuseVirtualPair helper for combo row synthesis"
```

---

### Task 2: Wire `opts.fuseVirtualPairs` into `buildTickerGroups`

**Files:**

- Modify: `web/lib/portfolioByStructure.ts` (signature + fusion pass)
- Modify: `web/tests/portfolio-by-structure.test.ts` (extend with new path)

- [ ] **Step 1: Add failing test for fused-path behavior**

Append to `web/tests/portfolio-by-structure.test.ts` (use the file's existing helpers — `mkSingle` is already in scope per the header comment):

```ts
describe("buildTickerGroups with fuseVirtualPairs: true", () => {
  beforeEach(() => __resetMissWarningsForTests());

  it("replaces two paired single-leg positions with one fused multi-leg position", () => {
    const longPut = mkSingle("TSLA", {
      type: "Put",
      dir: "LONG",
      strike: 400,
      expiry: "2027-01-15",
      mv: 3800,
      ec: 4000,
    });
    const shortPut = mkSingle("TSLA", {
      type: "Put",
      dir: "SHORT",
      strike: 390,
      expiry: "2027-01-15",
      mv: -200,
      ec: -500,
    });
    // Contract counts must match for pairing — mkSingle defaults should already match; adjust if not.
    shortPut.legs[0].contracts = longPut.legs[0].contracts;
    shortPut.contracts = longPut.contracts;

    const [group] = buildTickerGroups([longPut, shortPut], undefined, {
      fuseVirtualPairs: true,
    });
    const verticals = group.optionsByCategory.get("vertical") ?? [];

    expect(verticals).toHaveLength(1); // fused, not 2 leaves
    expect(verticals[0].legs).toHaveLength(2); // multi-leg
    expect(verticals[0].id).toBeLessThan(0); // synthetic id
    expect(verticals[0].structure_type).toBe("Bear Put Spread");
    expect(verticals[0].direction).toBe("DEBIT");
    expect(group.virtualPairs.has(verticals[0].id)).toBe(true); // pair map keyed by synthetic id
    expect(group.virtualPairs.has(longPut.id)).toBe(false); // originals no longer in tree
  });

  it("preserves ticker header aggregates (sum-invariant)", () => {
    const longPut = mkSingle("TSLA", {
      type: "Put",
      dir: "LONG",
      strike: 400,
      expiry: "2027-01-15",
      mv: 3800,
      ec: 4000,
    });
    const shortPut = mkSingle("TSLA", {
      type: "Put",
      dir: "SHORT",
      strike: 390,
      expiry: "2027-01-15",
      mv: -200,
      ec: -500,
    });
    shortPut.legs[0].contracts = longPut.legs[0].contracts;
    shortPut.contracts = longPut.contracts;

    const baseline = buildTickerGroups([longPut, shortPut]);
    const fused = buildTickerGroups([longPut, shortPut], undefined, {
      fuseVirtualPairs: true,
    });

    expect(fused[0].agg.mv).toBe(baseline[0].agg.mv);
    expect(fused[0].agg.totalPnl).toBe(baseline[0].agg.totalPnl);
    expect(fused[0].agg.entryCost).toBe(baseline[0].agg.entryCost);
  });

  it("leaves unpaired single legs as single-leg positions", () => {
    const loneShort = mkSingle("TSLA", {
      type: "Put",
      dir: "SHORT",
      strike: 400,
      expiry: "2027-01-15",
      mv: -100,
      ec: -200,
    });

    const [group] = buildTickerGroups([loneShort], undefined, {
      fuseVirtualPairs: true,
    });
    const singles = group.optionsByCategory.get("single") ?? [];
    expect(singles).toHaveLength(1);
    expect(singles[0].id).toBe(loneShort.id); // unchanged
    expect(singles[0].legs).toHaveLength(1);
  });

  it("default (fuseVirtualPairs omitted) produces unchanged output shape", () => {
    const longPut = mkSingle("TSLA", {
      type: "Put",
      dir: "LONG",
      strike: 400,
      expiry: "2027-01-15",
      mv: 3800,
      ec: 4000,
    });
    const shortPut = mkSingle("TSLA", {
      type: "Put",
      dir: "SHORT",
      strike: 390,
      expiry: "2027-01-15",
      mv: -200,
      ec: -500,
    });
    shortPut.legs[0].contracts = longPut.legs[0].contracts;
    shortPut.contracts = longPut.contracts;

    const [group] = buildTickerGroups([longPut, shortPut]);
    const verticals = group.optionsByCategory.get("vertical") ?? [];
    expect(verticals.map((p) => p.id).sort()).toEqual(
      [longPut.id, shortPut.id].sort(),
    );
  });
});
```

- [ ] **Step 2: Run — expect failure**

Run: `cd web && npm test -- portfolio-by-structure`
Expected: new tests FAIL (buildTickerGroups doesn't accept `opts`, or accepts but ignores it).

- [ ] **Step 3: Implement the fusion pass in `buildTickerGroups`**

Edit `web/lib/portfolioByStructure.ts`. Change the signature and add fusion:

```ts
export function buildTickerGroups(
  positions: PortfolioPosition[],
  prices?: Record<string, PriceData>,
  opts?: { fuseVirtualPairs?: boolean },
): TickerGroup[] {
  // ... existing Phase 1 bucketing unchanged up to the per-bucket loop ...
```

Inside the per-bucket `for (const b of buckets.values()) { ... }` loop, replace the block that starts with `const combos = detectVirtualCombos(b.options);` through the `virtualPairs` map population with:

```ts
// Virtual-combo detection: pair orphan single-leg options.
const combos = detectVirtualCombos(b.options);
const virtualPairs = new Map<number, VirtualPair>();

if (opts?.fuseVirtualPairs) {
  // Group pair members by pairKey, synthesize fused multi-leg positions,
  // and rewrite b.options so downstream code sees combos, not legs.
  const byPairKey = new Map<string, PortfolioPosition[]>();
  for (const pos of b.options) {
    const detection = combos.get(pos.id);
    if (!detection) continue;
    const list = byPairKey.get(detection.pair.pairKey) ?? [];
    list.push(pos);
    byPairKey.set(detection.pair.pairKey, list);
  }

  const fusedById = new Map<number, PortfolioPosition>();
  const consumedLegIds = new Set<number>();
  let fuseSeq = 0;
  for (const [, members] of byPairKey) {
    if (members.length !== 2) continue; // defensive — pair detector always emits 2
    const detection = combos.get(members[0].id)!;
    const fused = fuseVirtualPair(
      members[0],
      members[1],
      detection.pair,
      fuseSeq++,
    );
    fusedById.set(fused.id, fused);
    virtualPairs.set(fused.id, detection.pair);
    consumedLegIds.add(members[0].id);
    consumedLegIds.add(members[1].id);
  }

  // Rewrite: keep non-paired options, append fused positions.
  b.options = [
    ...b.options.filter((p) => !consumedLegIds.has(p.id)),
    ...fusedById.values(),
  ];
} else {
  for (const [posId, detection] of combos.entries()) {
    virtualPairs.set(posId, detection.pair);
  }
}
```

Then — still inside the same per-bucket loop — the existing sub-grouping pass uses `combos.get(pos.id)` to pick the override category. For fused positions (synthetic ids), `combos` has no entry, but we need the category to still land in `"vertical"` / `"straddle"` / `"strangle"` / `"synthetic"`. Extend the sub-grouping loop:

```ts
// Existing loop, modified:
for (const pos of b.options) {
  let category: CategoryKey;
  const override = combos.get(pos.id);
  if (override) {
    category = override.category;
  } else if (pos.id < 0) {
    // Fused virtual pair — look up category from its (now canonical) structure_type.
    category = getStructureCategory(resolveStructureKey(pos));
  } else {
    category = getStructureCategory(resolveStructureKey(pos));
  }
  let list = byCategory.get(category);
  if (!list) {
    list = [];
    byCategory.set(category, list);
  }
  list.push(pos);
}
```

(The `pos.id < 0` branch is effectively equivalent to the fallthrough for real positions — keep it as a comment-only distinction or collapse into the else. Either is fine as long as the lookup uses `resolveStructureKey(pos)`.)

Also extend the `allPositions` construction below it so header aggregates include the fused positions instead of the consumed legs — this happens automatically because `b.options` was rewritten. Verify by reading the final `allPositions` line — no changes needed if it reads from `b.options`.

- [ ] **Step 4: Run tests — expect pass**

Run: `cd web && npm test -- portfolio-by-structure`
Expected: all existing tests still pass; 4 new `fuseVirtualPairs: true` tests pass.

- [ ] **Step 5: Typecheck**

Run: `cd web && npm run typecheck`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add web/lib/portfolioByStructure.ts web/tests/portfolio-by-structure.test.ts
git commit -m "feat(portfolio): opt-in virtual-pair fusion in buildTickerGroups"
```

---

### Task 3: Opt in from `PortfolioByStructure.tsx` + suppress redundant label

**Files:**

- Modify: `web/components/PortfolioByStructure.tsx`
- Create: `web/tests/portfolio-by-structure-futu-combo.test.tsx`

- [ ] **Step 1: Write failing component test**

Create `web/tests/portfolio-by-structure-futu-combo.test.tsx`:

```tsx
import { describe, it, expect } from "vitest";
import { render, screen, within } from "@testing-library/react";
import PortfolioByStructure from "@/components/PortfolioByStructure";
import type { PortfolioPosition, PortfolioLeg } from "@/lib/types";

function mkLeg(o: Partial<PortfolioLeg>): PortfolioLeg {
  return {
    direction: "LONG",
    contracts: 10,
    type: "Put",
    strike: 400,
    entry_cost: 0,
    avg_cost: 0,
    market_price: null,
    market_value: null,
    market_price_is_calculated: false,
    ...o,
  };
}
function mkPos(
  id: number,
  leg: PortfolioLeg,
  o: Partial<PortfolioPosition> = {},
): PortfolioPosition {
  return {
    id,
    ticker: "TSLA",
    structure: `${leg.direction === "LONG" ? "Long" : "Short"} ${leg.type}`,
    structure_type: `${leg.direction === "LONG" ? "Long" : "Short"} ${leg.type}`,
    risk_profile: "",
    expiry: "2027-01-15",
    contracts: leg.contracts,
    direction: leg.direction,
    entry_cost: leg.entry_cost,
    max_risk: null,
    market_value: leg.market_value,
    legs: [leg],
    ib_daily_pnl: null,
    kelly_optimal: null,
    target: null,
    stop: null,
    entry_date: "2026-01-01",
    ...o,
  };
}

describe("PortfolioByStructure — Futu tab combo rendering", () => {
  const longPut = mkPos(
    1,
    mkLeg({
      direction: "LONG",
      type: "Put",
      strike: 400,
      contracts: 10,
      entry_cost: 4000,
      market_value: 3800,
    }),
  );
  const shortPut = mkPos(
    2,
    mkLeg({
      direction: "SHORT",
      type: "Put",
      strike: 390,
      contracts: 10,
      entry_cost: -500,
      market_value: -200,
    }),
  );

  it("renders one combo row under Futu with DEBIT/CREDIT badge", () => {
    render(
      <PortfolioByStructure
        positions={[longPut, shortPut]}
        activeAccount="futu"
        lastSync={new Date().toISOString()}
      />,
    );

    // Exactly one vertical category row (the fused combo), not two leaf rows at top level.
    // Sanity: at least one DEBIT or CREDIT badge is present.
    const badges = screen.getAllByText(/^(DEBIT|CREDIT|FLAT)$/);
    expect(badges.length).toBeGreaterThanOrEqual(1);
  });

  it("does NOT repeat the pair label as a text header when fused (redundant)", () => {
    render(
      <PortfolioByStructure
        positions={[longPut, shortPut]}
        activeAccount="futu"
        lastSync={new Date().toISOString()}
      />,
    );
    // The label appears inside the combo row's structure field, but the
    // redundant sub-group <div> above the table must be suppressed.
    // Assertion: query for a standalone label element with the exact class
    // used today — it should not be present.
    const stray = screen
      .queryAllByText(/Bear Put Spread \$390\/\$400/)
      .filter(
        (el) => el.className.includes("cell-muted") && el.tagName === "DIV",
      );
    expect(stray).toHaveLength(0);
  });

  it("still renders pair label header on IB tab (unchanged behavior)", () => {
    render(
      <PortfolioByStructure
        positions={[longPut, shortPut]}
        activeAccount="ib"
        lastSync={new Date().toISOString()}
      />,
    );
    // IB path: fusion is off, the label header div is still present.
    const label = screen
      .queryAllByText(/Bear Put Spread \$390\/\$400/)
      .filter(
        (el) => el.className.includes("cell-muted") && el.tagName === "DIV",
      );
    expect(label.length).toBeGreaterThanOrEqual(1);
  });
});
```

- [ ] **Step 2: Run — expect failure**

Run: `cd web && npm test -- portfolio-by-structure-futu-combo`
Expected: FAIL — either the combo row or the label-suppression assertion will not yet match.

- [ ] **Step 3: Wire the opt-in + suppress label**

Edit `web/components/PortfolioByStructure.tsx`:

3a. Update the `buildTickerGroups` call on line 59:

```ts
const groups = useMemo(
  () =>
    buildTickerGroups(positions, prices, {
      fuseVirtualPairs: activeAccount === "futu",
    }),
  [positions, prices, activeAccount],
);
```

3b. Suppress the redundant label when fusion consumed the pair. Inside the sub-group render (the block around lines 188–206), change:

```tsx
{subGroups.map((sg) => (
  <div key={sg.pairKey} data-pair-key={sg.pairKey}>
    {sg.label ? (
      <div
        className="cell-muted"
        style={{ fontSize: "11px", padding: "6px 0 2px 18px", letterSpacing: "0.02em" }}
      >
        {sg.label}
      </div>
    ) : null}
    <PositionTable
      positions={sg.positions}
      ...
    />
  </div>
))}
```

to:

```tsx
{
  subGroups.map((sg) => {
    // When fusion is on, a virtual pair shows up as a single multi-leg
    // position in sg.positions — the combo row carries the label itself,
    // so the standalone text header above would be redundant.
    const isFusedCombo =
      sg.positions.length === 1 &&
      sg.positions[0].legs.length === 2 &&
      group.virtualPairs.has(sg.positions[0].id);
    const showLabel = sg.label && !isFusedCombo;
    return (
      <div key={sg.pairKey} data-pair-key={sg.pairKey}>
        {showLabel ? (
          <div
            className="cell-muted"
            style={{
              fontSize: "11px",
              padding: "6px 0 2px 18px",
              letterSpacing: "0.02em",
            }}
          >
            {sg.label}
          </div>
        ) : null}
        <PositionTable
          positions={sg.positions}
          showUnderlying={true}
          prices={prices}
          readonly={readonly}
          hideHeader={takeHeaderSlot()}
        />
      </div>
    );
  });
}
```

- [ ] **Step 4: Run component test — expect pass**

Run: `cd web && npm test -- portfolio-by-structure-futu-combo`
Expected: 3 tests pass.

- [ ] **Step 5: Run full Vitest suite — guard against regressions**

Run: `cd web && npm test`
Expected: all tests pass.

- [ ] **Step 6: Typecheck**

Run: `cd web && npm run typecheck`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add web/components/PortfolioByStructure.tsx web/tests/portfolio-by-structure-futu-combo.test.tsx
git commit -m "feat(portfolio): render Futu combo rows (parity with IB tab)"
```

---

### Task 4: E2E Playwright verification

**Files:**

- Create: `web/e2e/futu-combo-presentation.spec.ts`

- [ ] **Step 1: Write the E2E spec**

Create `web/e2e/futu-combo-presentation.spec.ts`, modeled on `web/e2e/futu-readonly.spec.ts` (same mocking approach — intercept `/api/futu/portfolio` GET+POST and return a deterministic envelope containing two paired single-leg positions that form a Bear Put Spread). Use `installMocks`-style helpers if available in the existing spec; otherwise inline them.

```ts
import { test, expect, type Page } from "@playwright/test";

/**
 * Futu tab combo presentation parity with IB:
 * Two paired single-leg positions (Long Put $400 + Short Put $390 at the
 * same expiry, equal contract counts) should collapse into ONE combo row
 * with a DEBIT/CREDIT badge, matching how IB BAG positions render.
 */

async function installFutuMocks(page: Page) {
  // Deterministic two-leg Bear Put Spread on TSLA.
  const now = new Date().toISOString();
  const envelope = {
    source: "futu",
    last_sync: now,
    summary: {},
    positions: [
      {
        ticker: "TSLA",
        contract_type: "OPTION",
        option_type: "PUT",
        strike_price: 400,
        expiry_date: "2027-01-15",
        quantity: 5,
        avg_cost: 40,
        market_price: 38,
        market_value: 1900,
      },
      {
        ticker: "TSLA",
        contract_type: "OPTION",
        option_type: "PUT",
        strike_price: 390,
        expiry_date: "2027-01-15",
        quantity: -5,
        avg_cost: 10,
        market_price: 8,
        market_value: -400,
      },
    ],
  };
  await page.route("**/api/futu/portfolio", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(envelope),
    });
  });
  // IB + orders mocks so tab switching has content (follow futu-readonly.spec conventions).
  await page.route("**/api/portfolio", (r) =>
    r.fulfill({
      status: 200,
      body: JSON.stringify({ positions: [], last_sync: now }),
    }),
  );
  await page.route("**/api/orders", (r) =>
    r.fulfill({
      status: 200,
      body: JSON.stringify({ open: [], executed: [] }),
    }),
  );
  await page.route("**/api/ib-status", (r) =>
    r.fulfill({ status: 200, body: JSON.stringify({ connected: true }) }),
  );
}

test("Futu tab renders paired spread as a single combo row with DEBIT/CREDIT badge", async ({
  page,
}) => {
  await installFutuMocks(page);
  await page.goto("/portfolio");

  // Switch to Futu tab (selector mirrors futu-readonly.spec.ts).
  await page.getByRole("button", { name: /futu/i }).click();

  // Expect the combo row's structure field to show "Bear Put Spread …"
  await expect(page.getByText(/Bear Put Spread \$390\/\$400/)).toBeVisible();

  // Expect exactly one DEBIT badge for this ticker — asserting collapse.
  const badges = page.getByText(/^DEBIT$/);
  await expect(badges.first()).toBeVisible();

  // The redundant text header (duplicate label above the row) must NOT appear.
  // Structure field is INSIDE the row; the redundant <div class="cell-muted">
  // standalone label is what we suppressed in Task 3.
  const standaloneLabels = await page
    .locator("div.cell-muted", { hasText: /Bear Put Spread \$390\/\$400/ })
    .count();
  expect(standaloneLabels).toBe(0);
});
```

- [ ] **Step 2: Run the Playwright spec**

Run: `cd web && npx playwright test futu-combo-presentation --reporter=line`
Expected: 1 test passes.

If the test fails because the Futu tab UI selectors differ, inspect `web/e2e/futu-readonly.spec.ts` for the exact tab-selector pattern and port it into `installFutuMocks` / tab-click.

- [ ] **Step 3: Commit**

```bash
git add web/e2e/futu-combo-presentation.spec.ts
git commit -m "test(e2e): Futu combo row parity with IB tab"
```

---

### Task 5: Visual browser verification (required by `web/CLAUDE.md`)

**Files:** none

- [ ] **Step 1: Start the dev server**

Run: `cd web && npm run dev`
Wait for `http://localhost:3000` to respond.

- [ ] **Step 2: Visually verify both tabs**

Using chrome-cdp (primary) or Playwright browser:

1. Open `/portfolio`.
2. Select the IB tab — find a BAG combo (e.g. the SPX Bear Put Spread from the reference screenshot). Note the visual layout: single ticker row, DEBIT badge, aggregate qty/entry/MV/P&L, caret, two leg rows when expanded.
3. Select the Futu tab — find a paired spread (user's live snapshot has TSLA Bull Put and Bear Put spreads). Confirm the visual layout matches IB's: single row, DEBIT or CREDIT badge, aggregate numbers, caret reveals two legs.
4. Confirm solo (unpaired) Futu legs still render as flat single-leg rows.
5. Confirm no action buttons (edit/close) render on Futu rows — read-only enforcement is untouched.

- [ ] **Step 3: If visual parity is off, iterate**

If spacing, badge position, or row hierarchy differs from IB, read the closest BAG-rendered row in the IB tab's DOM and the fused row's DOM side-by-side. Typical culprits: missing `structure_type` aliases in `structureCatalog` (category falls through to `"other"`, placing the row in the wrong section). Fix inline and re-run Task 2's unit tests + this visual check.

- [ ] **Step 4: Stop the dev server**

Kill the `npm run dev` process.

No commit — this task is verification-only.

---

### Task 6: Final green check

- [ ] **Step 1: Full Vitest suite**

Run: `cd web && npm test`
Expected: all green.

- [ ] **Step 2: Typecheck**

Run: `cd web && npm run typecheck`
Expected: no errors.

- [ ] **Step 3: Playwright E2E suite (Futu-relevant)**

Run: `cd web && npx playwright test futu- --reporter=line`
Expected: all Futu-tagged specs pass, including the new one.

- [ ] **Step 4: Push branch / open PR (if branch work)**

If this ran in a worktree / feature branch, push and open a PR referencing the spec:

```bash
git push -u origin HEAD
gh pr create --title "Futu combo presentation parity with IB tab" --body "$(cat <<'EOF'
## Summary
- Add `fuseVirtualPair()` helper and `opts.fuseVirtualPairs` flag on `buildTickerGroups()`.
- `PortfolioByStructure` opts in for the Futu tab; IB path unchanged.
- Two paired single-leg Futu positions now render as one collapsible combo row with DEBIT/CREDIT badge, matching IB BAG presentation.

Spec: `docs/superpowers/specs/2026-04-24-futu-combo-presentation-design.md`

## Test plan
- [x] Vitest: `fuse-virtual-pair.test.ts`, `portfolio-by-structure.test.ts`, `portfolio-by-structure-futu-combo.test.tsx`
- [x] Playwright: `futu-combo-presentation.spec.ts`
- [x] Visual browser verification on both IB and Futu tabs
- [x] IB tab unchanged (regression sentinel test in place)
EOF
)"
```

---

## Acceptance

- Futu tab shows every detected virtual pair as one collapsible combo row with DEBIT/CREDIT badge and aggregate numbers.
- Expanding the combo reveals the two original legs.
- Unpaired Futu legs remain as flat single-leg rows.
- IB tab rendering is byte-identical to pre-change.
- No action buttons appear on Futu rows (read-only unchanged).
- All existing tests green; new tests green; typecheck green.
