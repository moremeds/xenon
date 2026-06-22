/**
 * Day Move breakdown — pure functions extracted from MetricCards.tsx
 * so they can be unit-tested without importing a React client component.
 */

import type { PortfolioData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import {
  legPriceKey,
  nativeToDisplayUsd,
  resolveRealtimePrice,
} from "@/lib/positionUtils";
import { fmtNative } from "@/lib/fx";
import type { PnlBreakdownRow } from "@/components/PnlBreakdownModal";

/**
 * Resolve the "current price" for a position's price data.
 *
 * Priority:
 *   1. `last` if it exists and is > 0
 *   2. `(bid + ask) / 2` if both bid and ask are defined and > 0
 *   3. null — position should be excluded from the Day Move calculation
 */
export function resolveLastOrMid(p: PriceData): number | null {
  return resolveRealtimePrice(p).price;
}

/** Returns true when the resolved price came from the mid (bid/ask), not last. */
function isMid(p: PriceData): boolean {
  return resolveRealtimePrice(p).isCalculated;
}

export function computeDayMoveBreakdown(
  portfolio: PortfolioData,
  prices: Record<string, PriceData>,
  usdPerUnit: Record<string, number> = { USD: 1 },
): { rows: PnlBreakdownRow[]; total: number } {
  let total = 0;
  const rows: PnlBreakdownRow[] = [];

  for (const pos of portfolio.positions) {
    // Foreign positions report ib_daily_pnl + native-venue quotes in JPY/KRW.
    // Convert every contribution to USD before summing; price columns render
    // in the native currency symbol. A position with no rate is excluded
    // (matches the backend fx_unconverted_count semantics) rather than leaking
    // a native magnitude into the USD total.
    const cur = (pos.currency || "USD").toUpperCase();
    const fmtCur =
      cur === "USD"
        ? (v: number) => `$${v.toFixed(2)}`
        : (v: number) => fmtNative(v, cur);
    if (pos.structure_type === "Stock") {
      const p = prices[pos.ticker];
      const current = p ? resolveLastOrMid(p) : null;
      if (current == null || p?.close == null || p.close <= 0) continue;

      const wsPnl = (current - p.close) * pos.contracts;
      const effectivePnl = pos.ib_daily_pnl != null ? pos.ib_daily_pnl : wsPnl;
      const pnlUsd = nativeToDisplayUsd(effectivePnl, cur, usdPerUnit);
      if (pnlUsd == null) continue;
      total += pnlUsd;
      const closeValue = p.close * pos.contracts;
      // % is a currency-invariant ratio — compute from the native pair.
      const pnlPct =
        closeValue !== 0 ? (effectivePnl / Math.abs(closeValue)) * 100 : null;
      const currentLabel = isMid(p)
        ? `${fmtCur(current)} (MID)`
        : fmtCur(current);
      rows.push({
        id: pos.id,
        ticker: pos.ticker,
        structure: pos.structure,
        col1: fmtCur(p.close),
        col2: currentLabel,
        pnl: pnlUsd,
        pnlPct,
      });
      continue;
    }

    let legPnl = 0;
    let allLegsValid = true;
    let closeStr = "";
    let lastStr = "";

    for (const leg of pos.legs) {
      const key = legPriceKey(pos.ticker, pos.expiry, leg);
      const lp = key ? prices[key] : null;
      const current = lp ? resolveLastOrMid(lp) : null;
      if (!lp || current == null || lp.close == null || lp.close <= 0) {
        allLegsValid = false;
        break;
      }
      const sign = leg.direction === "LONG" ? 1 : -1;
      legPnl += sign * (current - lp.close) * leg.contracts * 100;
      if (!closeStr) closeStr = `$${lp.close.toFixed(2)}`;
      if (!lastStr) {
        lastStr = isMid(lp)
          ? `$${current.toFixed(2)} (MID)`
          : `$${current.toFixed(2)}`;
      }
    }

    if (allLegsValid) {
      const effectivePnl = pos.ib_daily_pnl != null ? pos.ib_daily_pnl : legPnl;
      const pnlUsd = nativeToDisplayUsd(effectivePnl, cur, usdPerUnit);
      if (pnlUsd == null) continue;
      total += pnlUsd;
      rows.push({
        id: pos.id,
        ticker: pos.ticker,
        structure: pos.structure,
        col1: closeStr || "---",
        col2: lastStr || "---",
        pnl: pnlUsd,
        pnlPct: null,
      });
    }
  }

  return { rows, total };
}
