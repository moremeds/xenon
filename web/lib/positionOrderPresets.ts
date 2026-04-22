import type { PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import { legPriceKey } from "@/lib/positionUtils";

export type Intent = "close" | "add";

export type TicketPayload =
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
      expiry: string;
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
        expiry: string;
        strike: number;
        right: "C" | "P";
        action: "BUY" | "SELL";
        ratio: number;
      }>;
    };

export type TicketDraft = {
  payload: TicketPayload;
  /** Reference values for the UI's BID/MID/ASK quick buttons. May be null when quotes incomplete. */
  referenceBid: number | null;
  referenceMid: number | null;
  referenceAsk: number | null;
};

function pickStockBidAsk(p: PriceData | undefined | null): {
  bid: number | null;
  ask: number | null;
  mid: number | null;
  last: number | null;
} {
  if (!p) return { bid: null, ask: null, mid: null, last: null };
  const bid =
    p.bid != null && Number.isFinite(p.bid) && p.bid >= 0 ? p.bid : null;
  const ask =
    p.ask != null && Number.isFinite(p.ask) && p.ask > 0 ? p.ask : null;
  const last =
    p.last != null && Number.isFinite(p.last) && p.last > 0 ? p.last : null;
  const mid = bid != null && ask != null ? (bid + ask) / 2 : last;
  return { bid, ask, mid, last };
}

function round2(x: number): number {
  return Math.round(x * 100) / 100;
}

export function seedTicketFromPosition(
  position: PortfolioPosition,
  intent: Intent,
  prices: Record<string, PriceData>,
): TicketDraft {
  const sameDirection = intent === "add"; // add = same as position direction; close = opposite
  const isStock = position.structure_type === "Stock";
  const baseContracts = Math.abs(position.contracts);

  if (isStock) {
    const action: "BUY" | "SELL" = sameDirection
      ? position.direction === "LONG"
        ? "BUY"
        : "SELL"
      : position.direction === "LONG"
        ? "SELL"
        : "BUY";
    const q = pickStockBidAsk(prices[position.ticker]);
    const limitPrice = q.last ?? q.mid ?? 0;
    return {
      payload: {
        type: "stock",
        symbol: position.ticker,
        action,
        quantity: baseContracts,
        limitPrice,
        tif: "DAY",
      },
      referenceBid: q.bid,
      referenceMid: q.mid,
      referenceAsk: q.ask,
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
    const action: "BUY" | "SELL" = sameDirection
      ? position.direction === "LONG"
        ? "BUY"
        : "SELL"
      : position.direction === "LONG"
        ? "SELL"
        : "BUY";
    const key = legPriceKey(position.ticker, position.expiry, leg);
    const q = pickStockBidAsk(key ? prices[key] : null);
    return {
      payload: {
        type: "option",
        symbol: position.ticker,
        action,
        quantity: baseContracts,
        limitPrice: q.mid ?? 0,
        tif: "DAY",
        expiry,
        strike: leg.strike!,
        right,
      },
      referenceBid: q.bid,
      referenceMid: q.mid,
      referenceAsk: q.ask,
    };
  }

  // Combo
  const hasStockLeg = position.legs.some((l) => l.type === "Stock");
  if (hasStockLeg) {
    throw new Error(
      "Close/Add tickets for stock+option structures (Covered Call, Collar, Synthetic) are not yet supported",
    );
  }

  const expiry = position.expiry.replace(/-/g, "");
  const comboLegs = position.legs.map((leg) => {
    const right: "C" | "P" = leg.type === "Call" ? "C" : "P";
    // ComboLeg.action = spread structure, NOT trade direction. LONG → BUY, SHORT → SELL.
    const legAction: "BUY" | "SELL" = leg.direction === "LONG" ? "BUY" : "SELL";
    const ratio =
      baseContracts > 0
        ? Math.max(1, Math.round(Math.abs(leg.contracts) / baseContracts))
        : 1;
    return { expiry, strike: leg.strike!, right, action: legAction, ratio };
  });

  // Order.action: for "close" reverse the structure direction; for "add" match it.
  const orderAction: "BUY" | "SELL" = sameDirection
    ? position.direction === "LONG"
      ? "BUY"
      : "SELL"
    : position.direction === "LONG"
      ? "SELL"
      : "BUY";

  // Natural-market combo bid/ask. Always compute the BUY-combo cost and SELL-combo proceeds
  // from the structure's nominal LONG perspective (negate leg sign if position.direction is SHORT)
  // so that referenceMid is always the positive nominal value of the structure (debit/credit
  // both surface as positive prices; the Order.action carries the trade direction).
  const structureFlip = position.direction === "SHORT" ? -1 : 1;
  let buyComboCost = 0; // cost to BUY the (LONG-perspective) structure at market
  let sellComboProceeds = 0; // proceeds from SELLing the (LONG-perspective) structure at market
  let missing = false;
  for (const leg of position.legs) {
    const key = legPriceKey(position.ticker, position.expiry, leg);
    const lp = key ? prices[key] : null;
    if (!lp || lp.bid == null || lp.ask == null) {
      missing = true;
      break;
    }
    // Effective leg direction relative to the LONG perspective of the structure.
    const effectiveLong =
      (leg.direction === "LONG" ? 1 : -1) * structureFlip > 0;
    if (effectiveLong) {
      buyComboCost += lp.ask;
      sellComboProceeds += lp.bid;
    } else {
      buyComboCost -= lp.bid;
      sellComboProceeds -= lp.ask;
    }
  }

  let referenceBid: number | null = null;
  let referenceAsk: number | null = null;
  let referenceMid: number | null = null;
  if (!missing) {
    const lo = Math.min(sellComboProceeds, buyComboCost);
    const hi = Math.max(sellComboProceeds, buyComboCost);
    referenceBid = round2(lo);
    referenceAsk = round2(hi);
    referenceMid = round2((lo + hi) / 2);
  }

  // Seed limitPrice with the net mid. Combos may legitimately resolve to negative
  // (credit spreads) — the server route accepts non-zero numbers for combos.
  const limitPrice = referenceMid ?? 0;

  return {
    payload: {
      type: "combo",
      symbol: position.ticker,
      action: orderAction,
      quantity: baseContracts,
      limitPrice,
      tif: "DAY",
      legs: comboLegs,
    },
    referenceBid,
    referenceMid,
    referenceAsk,
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
