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

  throw new Error("Combo close tickets not yet implemented");
}
