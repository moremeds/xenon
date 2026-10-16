/**
 * useOrderPrices — Compute BID/MID/ASK for any order type
 *
 * Works for:
 * - Stock: uses ticker price data
 * - Single option: uses option-level price data
 * - Combo/spread: computes net prices from leg prices using natural market calculation
 */

import { useMemo } from "react";
import type { PriceData } from "@/lib/pricesProtocol";
import { optionKey } from "@/lib/pricesProtocol";
import {
  computeNetOptionQuote,
  normalizeComboOrder,
  type OrderLeg as ChainOrderLeg,
} from "@/lib/optionsChainUtils";
import type { OrderAction, OrderLeg, OrderPrices } from "../types";

interface UseOrderPricesOptions {
  ticker: string;
  type: "stock" | "option" | "combo";
  action: OrderAction;
  legs?: OrderLeg[];
  prices: Record<string, PriceData>;
  /** For single options: strike, right, expiry */
  option?: {
    strike: number;
    right: "C" | "P";
    expiry: string;
  };
}

/**
 * Compute order prices for any order type.
 *
 * For combos, delegates to computeNetOptionQuote — the single signed
 * natural-quote implementation (cross-fields; debit positive, credit
 * negative; independent of the order's BUY/SELL envelope action).
 */
export function useOrderPrices({
  ticker,
  type,
  legs,
  prices,
  option,
}: UseOrderPricesOptions): OrderPrices {
  return useMemo(() => {
    // Stock
    if (type === "stock") {
      const pd = prices[ticker];
      if (!pd || pd.bid == null || pd.ask == null) {
        return { bid: null, mid: null, ask: null, spread: null, spreadPct: null, available: false };
      }
      const spread = pd.ask - pd.bid;
      const mid = (pd.bid + pd.ask) / 2;
      return {
        bid: pd.bid,
        mid,
        ask: pd.ask,
        spread,
        spreadPct: mid > 0 ? (spread / mid) * 100 : null,
        available: true,
      };
    }

    // Single option
    if (type === "option" && option) {
      const key = optionKey({
        symbol: ticker,
        expiry: option.expiry.replace(/-/g, ""),
        strike: option.strike,
        right: option.right,
      });
      const pd = prices[key];
      if (!pd || pd.bid == null || pd.ask == null) {
        return { bid: null, mid: null, ask: null, spread: null, spreadPct: null, available: false };
      }
      const spread = pd.ask - pd.bid;
      const mid = (pd.bid + pd.ask) / 2;
      return {
        bid: pd.bid,
        mid,
        ask: pd.ask,
        spread,
        spreadPct: mid > 0 ? (spread / mid) * 100 : null,
        available: true,
      };
    }

    // Combo/spread — signed natural quote via the single implementation
    // (computeNetOptionQuote). The quote describes the structure itself
    // (debit positive / credit negative) and does NOT flip with the
    // envelope action: IB reverses leg actions on a SELL BAG, so the
    // market bid/ask/mid is action-independent.
    if (type === "combo" && legs && legs.length > 0) {
      const orderLegs: ChainOrderLeg[] = legs.map((leg) => ({
        id: leg.id,
        action: leg.direction === "LONG" ? "BUY" : "SELL",
        right: leg.type === "Call" ? "C" : "P",
        strike: leg.strike,
        expiry: leg.expiry.replace(/-/g, ""),
        quantity: Math.max(1, Math.trunc(leg.quantity ?? 1)),
        limitPrice: null,
      }));
      const quote = computeNetOptionQuote(
        normalizeComboOrder(orderLegs).legs,
        prices,
        ticker,
      );
      if (quote.bid == null || quote.ask == null || quote.mid == null) {
        return { bid: null, mid: null, ask: null, spread: null, spreadPct: null, available: false };
      }

      const spread = quote.ask - quote.bid;
      return {
        bid: quote.bid,
        mid: quote.mid,
        ask: quote.ask,
        spread,
        spreadPct: quote.mid > 0 ? (spread / quote.mid) * 100 : null,
        available: true,
      };
    }

    // Default: no prices available
    return { bid: null, mid: null, ask: null, spread: null, spreadPct: null, available: false };
  }, [ticker, type, legs, prices, option]);
}
