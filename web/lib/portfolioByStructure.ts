/**
 * Pure grouping function for the Portfolio "By Structure" view.
 *
 * Buckets positions by underlying ticker; inside each ticker the stock leg
 * (if any) is pinned and options are sub-grouped by catalog category. All
 * header aggregates (MV, day P&L, total P&L, Δ) reuse the SAME helpers the
 * row renderer uses so the card totals match the rows beneath them:
 *
 *   MV / total P&L  → getDisplayMarketValue / getDisplayTotalPnl
 *   Day P&L         → getTodayPnlDollars
 *   Net delta       → positionDeltaForHeader (known/unknown signal)
 *
 * Null propagation: a contributor with a null MV is skipped in the sum; the
 * aggregate MV is `null` iff every contributor was null. Same rule for
 * day / total P&L. Net delta is `null` iff every contributor came back with
 * `{ known: false }`.
 *
 * Sort: tickers by descending `|agg.mv ?? 0|`, stable on ties (insertion
 * order preserved via Array.sort stability, first-seen wins).
 */

import type { PortfolioPosition, PortfolioLeg } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import {
  getDisplayMarketValue,
  getDisplayTotalPnl,
  getTodayPnlDollars,
  resolveEntryCost,
} from "@/lib/positionUtils";
import { positionDeltaForHeader } from "@/lib/exposureBreakdown";
import {
  CATEGORY_ORDER,
  getStructureCategory,
  resolveStructureKey,
  type CategoryKey,
} from "@/lib/structureCatalog";

export type VirtualPair = {
  /** Stable identifier shared by every member position of the pair. */
  pairKey: string;
  /** Human-readable label, e.g. "Bull Put Spread $340/$350 · 2026-06-18". */
  label: string;
};

export type TickerGroup = {
  ticker: string;
  stock: PortfolioPosition | null;
  optionsByCategory: Map<CategoryKey, PortfolioPosition[]>;
  /**
   * For positions that were reclassified by virtual-combo detection, maps
   * position.id → pair metadata. Positions not in this map were either
   * real pre-classified combos (e.g. IB-grouped Bull Call Spread) or
   * unpaired single legs.
   */
  virtualPairs: Map<number, VirtualPair>;
  agg: {
    mv: number | null;
    entryCost: number;
    dayPnl: number | null;
    totalPnl: number | null;
    totalPnlPct: number | null;
    netDelta: number | null;
  };
  last: number | null;
  dayChgPct: number | null;
};

type Bucket = {
  ticker: string;
  order: number;
  stock: PortfolioPosition | null;
  options: PortfolioPosition[];
};

/**
 * Virtual combo detection for separately-held single-leg options.
 *
 * IB only merges legs into a single position when they were opened as one
 * order. Two separately-placed single-leg options on the same ticker+expiry
 * stay as two positions with `structure_type` "Long Call" / "Short Put"
 * etc. — even when they visually form a vertical, straddle, or synthetic.
 *
 * This pass pairs those orphan single-leg positions and overrides their
 * catalog category so the By Structure view groups them together. It does
 * NOT merge the positions (they still render as separate rows — IB tracks
 * them separately for P&L).
 *
 * Rules (greedy pairing within each (ticker, expiry) group):
 *   1. Same leg type (Call/Call or Put/Put), opposite directions → vertical
 *   2. Call + Put, same direction LONG → straddle (same strike) or strangle
 *   3. Call + Put, same direction SHORT → straddle (same strike) or strangle
 *   4. Long Call + Short Put or Long Put + Short Call → synthetic
 *
 * Returns a map from `position.id` to the override category. Positions not
 * in the map fall through to the normal `getStructureCategory` lookup.
 */
type ComboDetection = {
  category: CategoryKey;
  pair: VirtualPair;
};

function fmtStrike(n: number | null): string {
  if (n == null) return "?";
  return `$${n.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
}

function verticalLabel(
  type: "Call" | "Put",
  longStrike: number | null,
  shortStrike: number | null,
  expiry: string,
): string {
  const ls = longStrike ?? 0;
  const ss = shortStrike ?? 0;
  let name: string;
  if (type === "Put") {
    name = ls < ss ? "Bull Put Spread" : "Bear Put Spread";
  } else {
    name = ls < ss ? "Bull Call Spread" : "Bear Call Spread";
  }
  const lo = Math.min(ls, ss);
  const hi = Math.max(ls, ss);
  return `${name} ${fmtStrike(lo)}/${fmtStrike(hi)} · ${expiry}`;
}

function detectVirtualCombos(
  options: PortfolioPosition[],
): Map<number, ComboDetection> {
  const overrides = new Map<number, ComboDetection>();
  let pairSeq = 0;
  // Only single-leg option positions with a resolvable strike are candidates.
  // Null strikes would produce "$0" labels and should never be paired.
  const candidates = options.filter((p) => {
    if (p.legs.length !== 1) return false;
    const leg = p.legs[0];
    if (leg.type !== "Call" && leg.type !== "Put") return false;
    if (leg.strike == null) return false;
    return true;
  });

  const byStrike = (a: PortfolioPosition, b: PortfolioPosition) =>
    (a.legs[0].strike ?? 0) - (b.legs[0].strike ?? 0);

  /** Contract counts must match exactly for a clean pair — otherwise the
   *  leftover contracts of the larger side would be silently hidden inside a
   *  virtual combo that doesn't represent their true risk. */
  const contractsEqual = (a: PortfolioPosition, b: PortfolioPosition) =>
    a.legs[0].contracts === b.legs[0].contracts;
  // Group by expiry (ticker is already fixed at this point).
  const byExpiry = new Map<string, PortfolioPosition[]>();
  for (const p of candidates) {
    const arr = byExpiry.get(p.expiry) ?? [];
    arr.push(p);
    byExpiry.set(p.expiry, arr);
  }

  const markPair = (
    a: PortfolioPosition,
    b: PortfolioPosition,
    category: CategoryKey,
    label: string,
  ) => {
    const pairKey = `vp-${++pairSeq}`;
    const pair: VirtualPair = { pairKey, label };
    overrides.set(a.id, { category, pair });
    overrides.set(b.id, { category, pair });
  };

  /**
   * Try to match elements of `sideA` against `sideB` in ascending-strike order.
   * Each match requires both sides to still be available AND contract counts
   * to match exactly. Leftovers stay in `available` for the next pass.
   */
  const pairSorted = (
    group: PortfolioPosition[],
    available: Set<number>,
    sideAFilter: (p: PortfolioPosition) => boolean,
    sideBFilter: (p: PortfolioPosition) => boolean,
    onMatch: (a: PortfolioPosition, b: PortfolioPosition) => void,
  ) => {
    const sideA = group
      .filter((p) => available.has(p.id) && sideAFilter(p))
      .sort(byStrike);
    const sideB = group
      .filter((p) => available.has(p.id) && sideBFilter(p))
      .sort(byStrike);
    let i = 0;
    let j = 0;
    while (i < sideA.length && j < sideB.length) {
      const a = sideA[i];
      const b = sideB[j];
      if (!available.has(a.id)) {
        i++;
        continue;
      }
      if (!available.has(b.id)) {
        j++;
        continue;
      }
      if (!contractsEqual(a, b)) {
        // Skip the smaller side so the larger one can try the next partner.
        if (a.legs[0].contracts < b.legs[0].contracts) i++;
        else j++;
        continue;
      }
      onMatch(a, b);
      available.delete(a.id);
      available.delete(b.id);
      i++;
      j++;
    }
  };

  for (const [expiry, group] of byExpiry.entries()) {
    const available = new Set(group.map((p) => p.id));

    // Pass 1 — verticals (same type, opposite direction, strike-sorted).
    for (const type of ["Call", "Put"] as const) {
      pairSorted(
        group,
        available,
        (p) => p.legs[0].type === type && p.legs[0].direction === "LONG",
        (p) => p.legs[0].type === type && p.legs[0].direction === "SHORT",
        (long, short) => {
          const label = verticalLabel(
            type,
            long.legs[0].strike,
            short.legs[0].strike,
            expiry,
          );
          markPair(long, short, "vertical", label);
        },
      );
    }

    // Pass 2 — straddle / strangle (same direction, opposite type, strike-sorted).
    for (const dir of ["LONG", "SHORT"] as const) {
      pairSorted(
        group,
        available,
        (p) => p.legs[0].type === "Call" && p.legs[0].direction === dir,
        (p) => p.legs[0].type === "Put" && p.legs[0].direction === dir,
        (c, p) => {
          const cs = c.legs[0].strike;
          const ps = p.legs[0].strike;
          const sameStrike = cs === ps;
          const lo = Math.min(cs ?? 0, ps ?? 0);
          const hi = Math.max(cs ?? 0, ps ?? 0);
          const name = sameStrike
            ? `${dir === "LONG" ? "Long" : "Short"} Straddle ${fmtStrike(cs)}`
            : `${dir === "LONG" ? "Long" : "Short"} Strangle ${fmtStrike(lo)}/${fmtStrike(hi)}`;
          markPair(
            c,
            p,
            sameStrike ? "straddle" : "strangle",
            `${name} · ${expiry}`,
          );
        },
      );
    }

    // Pass 3 — synthetic / risk reversal (strike-sorted)
    const buildSynthetic = (
      longLeg: PortfolioPosition,
      shortLeg: PortfolioPosition,
    ) => {
      const ls = longLeg.legs[0].strike;
      const ss = shortLeg.legs[0].strike;
      const base = ls === ss ? "Synthetic" : "Risk Reversal";
      const label = `${base} ${fmtStrike(ls)}/${fmtStrike(ss)} · ${expiry}`;
      markPair(longLeg, shortLeg, "synthetic", label);
    };
    pairSorted(
      group,
      available,
      (p) => p.legs[0].type === "Call" && p.legs[0].direction === "LONG",
      (p) => p.legs[0].type === "Put" && p.legs[0].direction === "SHORT",
      buildSynthetic,
    );
    pairSorted(
      group,
      available,
      (p) => p.legs[0].type === "Put" && p.legs[0].direction === "LONG",
      (p) => p.legs[0].type === "Call" && p.legs[0].direction === "SHORT",
      buildSynthetic,
    );
  }

  return overrides;
}

/**
 * Sum contributions preserving sign; null inputs are skipped. Returns null
 * iff every contributor was null.
 */
function sumOrNull(values: (number | null)[]): number | null {
  let acc = 0;
  let any = false;
  for (const v of values) {
    if (v == null) continue;
    acc += v;
    any = true;
  }
  return any ? acc : null;
}

/**
 * Structure-type derivation from a virtual pair's two legs. Returns the
 * canonical catalog name so downstream consumers (label, structureCatalog
 * lookup) round-trip cleanly.
 *
 * Verticals are classified by STRIKE comparison (the structural definition),
 * not by credit/debit. This matches `verticalLabel()` above so the pair's
 * label string and the fused `structure_type` never disagree.
 */
function deriveFusedStructureType(a: PortfolioLeg, b: PortfolioLeg): string {
  const sameType = a.type === b.type;
  const sameDir = a.direction === b.direction;

  if (sameType && !sameDir && (a.type === "Put" || a.type === "Call")) {
    const longLeg = a.direction === "LONG" ? a : b;
    const shortLeg = a.direction === "LONG" ? b : a;
    const ls = longLeg.strike ?? 0;
    const ss = shortLeg.strike ?? 0;
    if (a.type === "Put")
      return ls < ss ? "Bull Put Spread" : "Bear Put Spread";
    return ls < ss ? "Bull Call Spread" : "Bear Call Spread";
  }
  if (!sameType && sameDir) {
    const sameStrike = a.strike === b.strike;
    const prefix = a.direction === "LONG" ? "Long" : "Short";
    return sameStrike ? `${prefix} Straddle` : `${prefix} Strangle`;
  }
  if (!sameType && !sameDir) {
    const sameStrike = a.strike === b.strike;
    return sameStrike ? "Synthetic" : "Risk Reversal";
  }
  throw new Error(
    `fuseVirtualPair: unexpected leg combination (same type + same direction) — this indicates a mismatch between detectVirtualCombos and fuseVirtualPair; caller contract broken.`,
  );
}

/**
 * Risk classification for a fused virtual pair. Verticals (all four
 * variants) are defined-risk; straddles / strangles / synthetics /
 * risk reversals are undefined-risk. Deriving from structure — not from
 * whichever leg the caller happened to pass first — ensures the pill
 * badge on the combo row agrees with the structure name.
 */
function deriveFusedRiskProfile(structureType: string): string {
  if (
    structureType.endsWith("Put Spread") ||
    structureType.endsWith("Call Spread")
  ) {
    return "defined";
  }
  return "undefined";
}

function orderFusedLegs(
  a: PortfolioLeg,
  b: PortfolioLeg,
): [PortfolioLeg, PortfolioLeg] {
  // Verticals / synthetics: LONG before SHORT.
  if (a.direction !== b.direction) {
    return a.direction === "LONG" ? [a, b] : [b, a];
  }
  // Straddle / strangle (same direction): strike ascending.
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
/** Synthetic ids are negative to avoid collisions with real broker-assigned
 *  position ids (IB and Futu both assign positive ids). The 1_000_000 offset
 *  leaves room below 0 for any future negative-id reservation. */
const SYNTHETIC_PAIR_ID_BASE = -1_000_000;

export function fuseVirtualPair(
  a: PortfolioPosition,
  b: PortfolioPosition,
  pair: VirtualPair,
  syntheticIdSeq: number,
): PortfolioPosition {
  const [legA, legB] = orderFusedLegs(a.legs[0], b.legs[0]);
  const entryCost = a.entry_cost + b.entry_cost;
  const structureType = deriveFusedStructureType(a.legs[0], b.legs[0]);
  const structure = `${a.ticker} ${pair.label}`;
  const marketValue = sumOrNull([a.market_value, b.market_value]);
  const ibDailyPnl = sumOrNull([
    a.ib_daily_pnl ?? null,
    b.ib_daily_pnl ?? null,
  ]);

  let direction: "DEBIT" | "CREDIT" | "FLAT";
  if (entryCost > 0) direction = "DEBIT";
  else if (entryCost < 0) direction = "CREDIT";
  else direction = "FLAT";

  // entry_date is ISO-8601 (YYYY-MM-DD or full timestamp) — lexical sort == chronological.
  const dates = [a.entry_date, b.entry_date]
    .filter((s) => s && s.length > 0)
    .sort();
  const entryDate = dates[0] ?? "";

  return {
    id: SYNTHETIC_PAIR_ID_BASE - syntheticIdSeq,
    ticker: a.ticker,
    structure,
    structure_type: structureType,
    risk_profile: deriveFusedRiskProfile(structureType),
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

export function buildTickerGroups(
  positions: PortfolioPosition[],
  prices?: Record<string, PriceData>,
  opts?: { fuseVirtualPairs?: boolean },
): TickerGroup[] {
  // Phase 1: bucket by ticker
  const buckets = new Map<string, Bucket>();
  let nextOrder = 0;
  for (const pos of positions) {
    const ticker = pos.ticker;
    let b = buckets.get(ticker);
    if (!b) {
      b = { ticker, order: nextOrder++, stock: null, options: [] };
      buckets.set(ticker, b);
    }
    // Discriminator: structure_type === "Stock" (NOT risk_profile — spec ISSUE)
    if (pos.structure_type === "Stock") {
      // First stock wins; subsequent stock rows (shouldn't happen) go to options bucket "other"
      if (!b.stock) b.stock = pos;
      else b.options.push(pos);
    } else {
      b.options.push(pos);
    }
  }

  // Phase 2: build TickerGroup for each bucket
  const groups: TickerGroup[] = [];
  for (const b of buckets.values()) {
    const allPositions: PortfolioPosition[] = [
      ...(b.stock ? [b.stock] : []),
      ...b.options,
    ];

    // Virtual-combo detection: pair orphan single-leg options that IB left
    // as separate positions (e.g. Long Put + Short Put same expiry → vertical).
    const combos = detectVirtualCombos(b.options);
    const virtualPairs = new Map<number, VirtualPair>();
    const fusedCategoryById = new Map<number, CategoryKey>();

    if (opts?.fuseVirtualPairs) {
      // Group pair members by pairKey, synthesize fused multi-leg positions,
      // and rewrite b.options so downstream sub-grouping sees combos, not legs.
      const byPairKey = new Map<string, PortfolioPosition[]>();
      for (const pos of b.options) {
        const detection = combos.get(pos.id);
        if (!detection) continue;
        const list = byPairKey.get(detection.pair.pairKey) ?? [];
        list.push(pos);
        byPairKey.set(detection.pair.pairKey, list);
      }

      const fusedPositions: PortfolioPosition[] = [];
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
        fusedPositions.push(fused);
        virtualPairs.set(fused.id, detection.pair);
        // Preserve the detector's category for the fused position so the
        // sub-grouping loop below lands it under the correct catalog bucket
        // (e.g. "synthetic" for Synthetic / Risk Reversal, which have no
        // catalog entry by name and would otherwise fall through to "other").
        fusedCategoryById.set(fused.id, detection.category);
        consumedLegIds.add(members[0].id);
        consumedLegIds.add(members[1].id);
      }

      b.options = [
        ...b.options.filter((p) => !consumedLegIds.has(p.id)),
        ...fusedPositions,
      ];
    } else {
      for (const [posId, detection] of combos.entries()) {
        virtualPairs.set(posId, detection.pair);
      }
    }

    // Sub-group options by category, preserving CATEGORY_ORDER
    const byCategory = new Map<CategoryKey, PortfolioPosition[]>();
    for (const pos of b.options) {
      const override = combos.get(pos.id);
      const category =
        override?.category ??
        fusedCategoryById.get(pos.id) ??
        getStructureCategory(resolveStructureKey(pos));
      let list = byCategory.get(category);
      if (!list) {
        list = [];
        byCategory.set(category, list);
      }
      list.push(pos);
    }
    // Re-emit in canonical CATEGORY_ORDER for deterministic iteration
    const orderedByCategory = new Map<CategoryKey, PortfolioPosition[]>();
    for (const cat of CATEGORY_ORDER) {
      const list = byCategory.get(cat);
      if (list && list.length > 0) orderedByCategory.set(cat, list);
    }

    // Aggregate header values
    const mvs = allPositions.map((p) => getDisplayMarketValue(p, prices));
    const pnls = allPositions.map((p) => getDisplayTotalPnl(p, prices));
    const dayPnls = allPositions.map((p) =>
      p.structure_type === "Stock"
        ? (() => {
            const lp = prices?.[p.ticker];
            if (
              lp?.last != null &&
              lp.last > 0 &&
              lp.close != null &&
              lp.close > 0
            ) {
              return (lp.last - lp.close) * p.contracts;
            }
            return null;
          })()
        : getTodayPnlDollars(p, prices),
    );

    const mv = sumOrNull(mvs);
    const totalPnl = sumOrNull(pnls);
    const dayPnl = sumOrNull(dayPnls);

    // Total entry cost across ALL positions (the bankroll side of %).
    const entryCost = allPositions.reduce((s, p) => s + resolveEntryCost(p), 0);

    // Per Codex review: the % denominator must match the numerator's cohort.
    // If any P&L was null-skipped, the full entry cost would overstate the
    // denominator → null the percentage instead of publishing a wrong number.
    const pnlSubsetResolved = pnls.every((v) => v != null);
    const totalPnlPct =
      totalPnl != null && pnlSubsetResolved && entryCost !== 0
        ? (totalPnl / Math.abs(entryCost)) * 100
        : null;

    // Net delta: null iff EITHER every contributor was unknown OR any
    // contributor was unknown (partial sums mislead traders into thinking
    // the header is precise). Strict policy per Codex review — if any leg
    // is partial the whole header is `—`.
    let netDeltaSum = 0;
    let anyKnown = false;
    let allKnown = true;
    for (const p of allPositions) {
      const { signed, known } = positionDeltaForHeader(p, prices);
      if (signed != null) netDeltaSum += signed;
      if (known) anyKnown = true;
      else allKnown = false;
    }
    const netDelta = anyKnown && allKnown ? netDeltaSum : null;

    // Underlying spot + day chg%
    const underlyingLast = prices?.[b.ticker]?.last ?? null;
    const underlyingClose = prices?.[b.ticker]?.close ?? null;
    const last =
      underlyingLast != null && underlyingLast > 0 ? underlyingLast : null;
    const dayChgPct =
      last != null && underlyingClose != null && underlyingClose > 0
        ? ((last - underlyingClose) / underlyingClose) * 100
        : null;

    groups.push({
      ticker: b.ticker,
      stock: b.stock,
      optionsByCategory: orderedByCategory,
      virtualPairs,
      agg: { mv, entryCost, dayPnl, totalPnl, totalPnlPct, netDelta },
      last,
      dayChgPct,
    });
  }

  // Phase 3: sort by |mv| desc, stable on ties
  groups.sort((a, b) => {
    const am = Math.abs(a.agg.mv ?? 0);
    const bm = Math.abs(b.agg.mv ?? 0);
    return bm - am;
  });

  return groups;
}
