import type { PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";

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

  throw new Error("Non-stock close tickets not yet implemented");
}
