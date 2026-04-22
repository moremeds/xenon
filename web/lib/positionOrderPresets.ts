import type { PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import { legPriceKey } from "@/lib/positionUtils";

/**
 * Payload shape matches POST /api/orders/place. Mirrors the shapes produced
 * by `buildSingleLegOrderPayload` and the combo form in OrderTab.tsx.
 */
export type ClosePayload =
  | {
      type: "stock";
      symbol: string;
      action: "BUY" | "SELL";
      quantity: number;
      limitPrice: number;
      tif: "DAY" | "GTC";
    }
  | {
      type: "option";
      symbol: string;
      action: "BUY" | "SELL";
      quantity: number;
      limitPrice: number;
      tif: "DAY" | "GTC";
      expiry: string; // YYYYMMDD
      strike: number;
      right: "C" | "P";
    }
  | {
      type: "combo";
      symbol: string;
      action: "BUY" | "SELL";
      quantity: number;
      limitPrice: number;
      tif: "DAY" | "GTC";
      legs: Array<{
        expiry: string; // YYYYMMDD
        strike: number;
        right: "C" | "P";
        action: "BUY" | "SELL";
        ratio: number;
      }>;
    };

export type CloseTicketDraft = {
  payload: ClosePayload;
  /** Midpoint reference for UI display (may differ from payload.limitPrice after edits). */
  referenceMid: number | null;
};

function midFromQuote(p: PriceData | undefined | null): number | null {
  if (!p) return null;
  // Prefer bid/ask mid (accepts valid 0-bid OTM options where bid=0, ask>0).
  if (
    p.bid != null &&
    p.ask != null &&
    Number.isFinite(p.bid) &&
    Number.isFinite(p.ask) &&
    p.bid >= 0 &&
    p.ask > 0
  ) {
    return (p.bid + p.ask) / 2;
  }
  if (p.last != null && Number.isFinite(p.last) && p.last > 0) return p.last;
  return null;
}

export function buildCloseTicket(
  position: PortfolioPosition,
  prices: Record<string, PriceData>,
): CloseTicketDraft {
  const isStock = position.structure_type === "Stock";

  if (isStock) {
    const action: "BUY" | "SELL" =
      position.direction === "LONG" ? "SELL" : "BUY";
    const mid = midFromQuote(prices[position.ticker]);
    const limitPrice = mid ?? 0;
    return {
      payload: {
        type: "stock",
        symbol: position.ticker,
        action,
        quantity: Math.abs(position.contracts),
        limitPrice,
        tif: "DAY",
      },
      referenceMid: mid,
    };
  }

  const isSingleLegOption =
    position.legs.length === 1 &&
    position.legs[0].type !== "Stock" &&
    position.legs[0].strike != null;

  if (isSingleLegOption) {
    const leg = position.legs[0];
    const right: "C" | "P" = leg.type === "Call" ? "C" : "P";
    const expiry = position.expiry.replace(/-/g, "");
    const action: "BUY" | "SELL" =
      position.direction === "LONG" ? "SELL" : "BUY";
    // Use the shared helper so the key matches `legPriceKey(...)` used
    // elsewhere in the app (underscore-joined SYMBOL_YYYYMMDD_STRIKE_RIGHT).
    const key = legPriceKey(position.ticker, position.expiry, leg);
    const mid = key ? midFromQuote(prices[key]) : null;
    return {
      payload: {
        type: "option",
        symbol: position.ticker,
        action,
        quantity: Math.abs(position.contracts),
        limitPrice: mid ?? 0,
        tif: "DAY",
        expiry,
        strike: leg.strike!,
        right,
      },
      referenceMid: mid,
    };
  }

  // Combo (multi-leg)
  // NOTE: Covered Call / Collar / Synthetic include a Stock leg. The /api/orders/place
  // combo payload schema cannot express a stock leg (requires strike/expiry/right),
  // and the IB BAG builder only qualifies option legs. Reject up-front — a future
  // plan can add a hybrid stock+BAG ticket.
  const hasStockLeg = position.legs.some((l) => l.type === "Stock");
  if (hasStockLeg) {
    throw new Error(
      "Close tickets for stock+option structures (Covered Call, Collar, Synthetic) are not yet supported",
    );
  }

  const expiry = position.expiry.replace(/-/g, "");
  const baseContracts = Math.abs(position.contracts);
  const comboLegs = position.legs.map((leg) => {
    const right: "C" | "P" = leg.type === "Call" ? "C" : "P";
    // Per web/CLAUDE.md "IB Combo (BAG) Order Leg Convention":
    // ComboLeg.action = spread structure (LONG → BUY, SHORT → SELL), NOT trade direction.
    const legAction: "BUY" | "SELL" = leg.direction === "LONG" ? "BUY" : "SELL";
    // Ratio is per-leg contracts / structure contracts. Guards 1×2s etc.
    const legContracts = Math.abs(leg.contracts);
    const ratio =
      baseContracts > 0
        ? Math.max(1, Math.round(legContracts / baseContracts))
        : 1;
    return {
      expiry,
      strike: leg.strike!,
      right,
      action: legAction,
      ratio,
    };
  });

  // Order.action: reverse of the structure's net direction.
  const orderAction: "BUY" | "SELL" =
    position.direction === "LONG" ? "SELL" : "BUY";

  // Net mid: sum of sign × leg_mid, where sign comes from the position (LONG=+1, SHORT=-1).
  // Keep in mind net can be negative (credit).
  let netMid: number | null = 0;
  let missing = false;
  for (const leg of position.legs) {
    const key = legPriceKey(position.ticker, position.expiry, leg);
    const legMid = key ? midFromQuote(prices[key]) : null;
    if (legMid == null) {
      missing = true;
      break;
    }
    const sign = leg.direction === "LONG" ? 1 : -1;
    netMid = (netMid as number) + sign * legMid;
  }
  const referenceMid = missing ? null : (netMid as number);

  return {
    payload: {
      type: "combo",
      symbol: position.ticker,
      action: orderAction,
      quantity: baseContracts,
      limitPrice: referenceMid ?? 0,
      tif: "DAY",
      legs: comboLegs,
    },
    referenceMid,
  };
}

/**
 * Apply a percentage chip to a qty, rounding half-up, with a min-1 clamp
 * when the source qty is non-zero. A 25% chip on 2 contracts would otherwise
 * round to 0, which would submit an empty order.
 */
export function applyQtyChip(fullQty: number, pct: number): number {
  if (fullQty <= 0) return 0;
  const raw = Math.round(fullQty * pct);
  return Math.max(1, raw);
}
