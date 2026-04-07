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

import type { PortfolioPosition } from "@/lib/types";
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

export type TickerGroup = {
  ticker: string;
  stock: PortfolioPosition | null;
  optionsByCategory: Map<CategoryKey, PortfolioPosition[]>;
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

export function buildTickerGroups(
  positions: PortfolioPosition[],
  prices?: Record<string, PriceData>,
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

    // Sub-group options by category, preserving CATEGORY_ORDER
    const byCategory = new Map<CategoryKey, PortfolioPosition[]>();
    for (const pos of b.options) {
      const key = resolveStructureKey(pos);
      const category = getStructureCategory(key);
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
            if (lp?.last != null && lp.last > 0 && lp.close != null && lp.close > 0) {
              return (lp.last - lp.close) * p.contracts;
            }
            return null;
          })()
        : getTodayPnlDollars(p, prices),
    );

    const mv = sumOrNull(mvs);
    const totalPnl = sumOrNull(pnls);
    const dayPnl = sumOrNull(dayPnls);
    const entryCost = allPositions.reduce((s, p) => s + resolveEntryCost(p), 0);
    const totalPnlPct =
      totalPnl != null && entryCost !== 0
        ? (totalPnl / Math.abs(entryCost)) * 100
        : null;

    // Net delta: only null iff every contributor came back { known: false }
    let netDeltaSum = 0;
    let anyKnown = false;
    for (const p of allPositions) {
      const { signed, known } = positionDeltaForHeader(p, prices);
      if (signed != null) netDeltaSum += signed;
      if (known) anyKnown = true;
    }
    const netDelta = anyKnown ? netDeltaSum : null;

    // Underlying spot + day chg%
    const underlyingLast = prices?.[b.ticker]?.last ?? null;
    const underlyingClose = prices?.[b.ticker]?.close ?? null;
    const last = underlyingLast != null && underlyingLast > 0 ? underlyingLast : null;
    const dayChgPct =
      last != null && underlyingClose != null && underlyingClose > 0
        ? ((last - underlyingClose) / underlyingClose) * 100
        : null;

    groups.push({
      ticker: b.ticker,
      stock: b.stock,
      optionsByCategory: orderedByCategory,
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
