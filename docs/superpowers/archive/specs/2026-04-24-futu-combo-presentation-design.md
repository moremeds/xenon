# Futu Tab — Combo Presentation (parity with IB tab)

**Date:** 2026-04-24
**Area:** `web/` — portfolio rendering
**Status:** Design approved, awaiting implementation plan

## Problem

The IB account tab renders a detected options combo (e.g. Bear Put Spread) as a single collapsible row — one ticker line with aggregate qty, DEBIT/CREDIT badge, net avg entry, entry cost, market value, day P&L, total P&L, expiry — and a caret to expand the two legs underneath.

The Futu account tab cannot do this today. It shows a text label header (`"Bull Put Spread $390/$400 · 2027-01-15"`) above two leaf rows for the individual Short Put / Long Put positions. Users see the same spread rendered two different ways depending on which tab they're on.

## Root cause

`PositionTable` already renders a multi-leg `PortfolioPosition` as one collapsible combo row. IB BAG positions arrive pre-fused (server-side), so they flow straight into that render path. Futu's API returns flat single-leg positions; `detectVirtualCombos()` in `web/lib/portfolioByStructure.ts` recognises the pair but only attaches a label — it does not fuse the two positions into one multi-leg object, so the renderer keeps drawing them as leaves.

The same gap exists on the IB tab for _legged-in_ spreads (two separate orders instead of a BAG), but IB order flows operate on the individual leg positions; changing that side risks regression. Futu is read-only (no order flows), so fusion is safe there unconditionally.

## Solution

Add a shared opt-in fusion pass to `buildTickerGroups()`. When enabled, virtual pairs collapse into one synthesized multi-leg `PortfolioPosition` before category sub-grouping. The Futu caller opts in; the IB caller does not. One code path, zero IB regression surface.

### Architecture

```
detectVirtualCombos()  →  pair metadata (unchanged)
                                │
          fuseVirtualPairs opt? │
                   ┌────────────┴────────────┐
                   │                         │
                 false                     true
                   │                         │
     two leg PortfolioPositions    fuseVirtualPair(a, b, pair)
     + label on sub-group           → one multi-leg PortfolioPosition
                   │                         │
                   └────────────┬────────────┘
                                ▼
                    buildTickerGroups → TickerGroup[]
                                ▼
                        PositionTable
                 (multi-leg → collapsible combo row,
                  single-leg → flat row — already works)
```

### Public contract change

```ts
// web/lib/portfolioByStructure.ts
export function buildTickerGroups(
  positions: PortfolioPosition[],
  prices?: Record<string, PriceData>,
  opts?: { fuseVirtualPairs?: boolean }, // default: false
): TickerGroup[];
```

Default `false` preserves existing behavior for every caller that doesn't opt in.

### Caller wiring

`web/components/PortfolioByStructure.tsx`:

```ts
const groups = useMemo(
  () =>
    buildTickerGroups(positions, prices, {
      fuseVirtualPairs: activeAccount === "futu",
    }),
  [positions, prices, activeAccount],
);
```

When fusion is on, the sub-group label (`sg.label`) becomes redundant for fused rows — the combo row now owns the title. Rule: suppress the label when the sub-group contains exactly one position whose `legs.length === 2` and whose id is in `virtualPairs`. Other sub-groups (solo unpaired legs, real BAGs) are unaffected.

### New pure function

```ts
// web/lib/portfolioByStructure.ts
function fuseVirtualPair(
  a: PortfolioPosition,
  b: PortfolioPosition,
  pair: VirtualPair,
  syntheticIdSeq: number,
): PortfolioPosition;
```

Invariants guaranteed by the caller (pair detector already enforces these):

- Same ticker, same expiry.
- `a.legs.length === 1 && b.legs.length === 1`.
- `a.legs[0].contracts === b.legs[0].contracts`.

Field-by-field synthesis:

| Field                             | Rule                                                                                                                                                                                                                                                                                           |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`                              | `-(1_000_000 + syntheticIdSeq)` — negative synthetic id, cannot collide with broker ids                                                                                                                                                                                                        |
| `ticker`, `expiry`                | Inherited (equal on both by invariant)                                                                                                                                                                                                                                                         |
| `contracts`                       | `a.legs[0].contracts`                                                                                                                                                                                                                                                                          |
| `legs`                            | For verticals/synthetics: `[LONG leg, SHORT leg]`. For straddles/strangles: sort by strike ascending.                                                                                                                                                                                          |
| `structure_type`                  | Derived from pair category + leg sides. Enumerated: `"Bull Put Spread"`, `"Bear Put Spread"`, `"Bull Call Spread"`, `"Bear Call Spread"`, `"Long Straddle"`, `"Short Straddle"`, `"Long Strangle"`, `"Short Strangle"`, `"Synthetic"`, `"Risk Reversal"`. All are known to `structureCatalog`. |
| `structure`                       | `"{ticker} {structure_type} $lo/$hi"` — matches IB BAG display format                                                                                                                                                                                                                          |
| `risk_profile`                    | Looked up from `structureCatalog.resolveStructureKey()` on the synthesized `structure_type`                                                                                                                                                                                                    |
| `direction`                       | `"DEBIT"` if `entry_cost > 0`, `"CREDIT"` if `< 0`, else `"FLAT"` — matches IB BAG convention                                                                                                                                                                                                  |
| `entry_cost`                      | `a.entry_cost + b.entry_cost` (signs preserve naturally — see sign invariant below)                                                                                                                                                                                                            |
| `market_value`                    | `sumOrNull([a.market_value, b.market_value])`                                                                                                                                                                                                                                                  |
| `ib_daily_pnl`                    | `sumOrNull([a.ib_daily_pnl, b.ib_daily_pnl])` — naturally `null` on Futu side                                                                                                                                                                                                                  |
| `max_risk`                        | `null` — combo-level risk computation lives elsewhere; don't synthesize                                                                                                                                                                                                                        |
| `kelly_optimal`, `target`, `stop` | `null` — per-trade fields, not pair-level                                                                                                                                                                                                                                                      |
| `entry_date`                      | Earliest non-empty of `a.entry_date`, `b.entry_date`; empty string if both empty (Futu case)                                                                                                                                                                                                   |
| `market_price_is_calculated`      | `true` if either leg has it `true`                                                                                                                                                                                                                                                             |

### Sign invariant (verified)

`web/lib/futuPortfolioAdapter.ts:198-201` confirms: `entry_cost = avg_cost * quantity * multiplier` with `quantity < 0` for SHORT → entry_cost is already negative for shorts. `market_value` is likewise pre-signed. Simple summation yields correct net DEBIT/CREDIT sign with no `abs()` calls.

This aligns with `web/CLAUDE.md` "Credit/Debit Sign Convention" — signs are preserved through the pipeline.

### Integration into `buildTickerGroups`

Insertion point: after `detectVirtualCombos()` runs on `b.options` (line 316 of current file). When `opts?.fuseVirtualPairs`:

1. Group pair members by `pair.pairKey`.
2. For each pair, call `fuseVirtualPair(...)`.
3. Replace the two original leg positions in `b.options` with the fused position.
4. Rebuild `allPositions` from the new `b.options` so header aggregates (MV, day P&L, etc.) stay consistent — the fused position's MV/P&L equal the sum of its legs, so ticker totals are unchanged.
5. Populate `virtualPairs` map keyed by the fused position's synthetic id (not the original leg ids, which no longer appear in the render tree).

When `fuseVirtualPairs` is `false` (default / IB path): behavior is byte-identical to today.

## Testing

All tests live under `web/` and run via Vitest unless noted.

### Unit — `fuseVirtualPair.test.ts` (new)

- Bull Put Spread (Short $390 / Long $400): asserts `structure_type`, CREDIT direction, net `entry_cost` sign, `market_value` sum, leg order `[LONG, SHORT]`, synthetic `id < 0`.
- Bear Put Spread (Long $400 / Short $390): DEBIT direction, correct leg order.
- Bull Call Spread, Bear Call Spread: same invariants.
- Long Straddle (same strike), Short Strangle (two strikes): asserts strike-sorted leg order.
- Long Synthetic, Risk Reversal: one case each.
- Null market_value propagation: one leg null → fused MV follows `sumOrNull` rule.
- `entry_date`: picks earliest non-empty; returns `""` when both empty.
- `market_price_is_calculated`: `true` if either leg has it.

### Unit — `portfolioByStructure.test.ts` (extend)

- New test block `buildTickerGroups({ fuseVirtualPairs: true })`:
  - Options bucket contains fused positions; original leg ids are absent.
  - `virtualPairs` map is keyed by synthetic ids.
  - Ticker header aggregates (MV, day P&L, total P&L) are unchanged vs the `false` path (fusion is sum-preserving).
- Sentinel: default call (`fuseVirtualPairs` omitted) produces the same output shape as before this change.

### Component — `PortfolioByStructure.futu.test.tsx` (new)

- Fixture with two single-leg Futu positions forming a Bear Put Spread.
- Assert: one combo row renders with DEBIT/CREDIT badge, qty, net entry, MV, P&L, expiry.
- Assert: label text `"Bear Put Spread …"` does NOT appear as a standalone sub-group header (it's consumed by the combo row title).
- Assert: expanding the combo row reveals two leg rows with correct strike / direction / qty / entry / MV.
- Assert: no action buttons (Futu is read-only).

### E2E — Playwright (extend existing Futu portfolio spec)

- Load `/portfolio`, switch to Futu tab.
- Pick a known paired spread from the fixture / live snapshot.
- Assert the combo row is present and visually structured like IB's combo row (one row, caret, DEBIT/CREDIT badge).
- Expand and assert leg rows appear.

Per root `CLAUDE.md`: UI changes are not done until visually confirmed in a browser.

## Non-goals

- IB tab behavior for legged-in spreads stays as-is. A future change can flip the flag once IB Close/Modify flows are verified to survive fusion.
- No backend/API changes. Futu adapter unchanged.
- No new CSS or token changes. The combo row already has correct visual treatment.
- No fusion for 3+ leg structures (iron condor, butterfly). Virtual-pair detection is 2-leg only today; expanding to 4-leg is a separate project.

## Files touched

| File                                                             | Change                                                                                                        |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `web/lib/portfolioByStructure.ts`                                | Add `opts.fuseVirtualPairs`, add `fuseVirtualPair()` pure fn, insert fusion pass before category sub-grouping |
| `web/components/PortfolioByStructure.tsx`                        | Pass `fuseVirtualPairs: activeAccount === "futu"`; suppress redundant sub-group label for fused rows          |
| `web/tests/fuseVirtualPair.test.ts`                              | New                                                                                                           |
| `web/tests/portfolioByStructure.test.ts`                         | Extend with `fuseVirtualPairs: true` cases                                                                    |
| `web/tests/PortfolioByStructure.futu.test.tsx`                   | New                                                                                                           |
| `web/playwright/futu-portfolio.spec.ts` (or existing equivalent) | Extend with combo-row assertion                                                                               |

## Open questions

None at design time. Implementation may surface edge cases around `structureCatalog` lookups for synthesized `structure_type` values — if a category doesn't round-trip cleanly, fall back to `"options_other"` risk_profile and flag for a catalog update.
