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
 * For combos, uses natural market calculation:
 *   - BUY combo: pay ASK on BUY legs, receive BID on SELL legs
 *   - SELL combo: receive BID on BUY legs, pay ASK on SELL legs
 */
export function useOrderPrices({
  ticker,
  type,
  action,
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

    // Combo/spread
    if (type === "combo" && legs && legs.length > 0) {
      let netBid = 0;
      let netAsk = 0;
      let allAvailable = true;

      for (const leg of legs) {
        const key = optionKey({
          symbol: ticker,
          expiry: leg.expiry.replace(/-/g, ""),
          strike: leg.strike,
          right: leg.type === "Call" ? "C" : "P",
        });
        const pd = prices[key];
        if (!pd || pd.bid == null || pd.ask == null) {
          allAvailable = false;
          break;
        }

        // Natural market calculation:
        // For BUY combo: BUY legs pay ask, SELL legs receive bid
        // For SELL combo: BUY legs receive bid, SELL legs pay ask
        // But since we're computing the combo's BID/ASK (not the execution direction),
        // we compute based on leg direction:
        //   LONG legs contribute positively
        //   SHORT legs contribute negatively
        // Then we apply action reversal to get effective direction

        const effectivelySelling = (action === "SELL") === (leg.direction === "LONG");

        if (effectivelySelling) {
          // Selling this leg: receive BID
          netBid += pd.bid;
          netAsk += pd.ask;
        } else {
          // Buying this leg: pay ASK
          netBid -= pd.ask;
          netAsk -= pd.bid;
        }
      }

      if (!allAvailable) {
        return { bid: null, mid: null, ask: null, spread: null, spreadPct: null, available: false };
      }

      const absBid = Math.abs(netBid);
      const absAsk = Math.abs(netAsk);
      const bid = Math.min(absBid, absAsk);
      const ask = Math.max(absBid, absAsk);
      const mid = (bid + ask) / 2;
      const spread = ask - bid;

      return {
        bid,
        mid,
        ask,
        spread,
        spreadPct: mid > 0 ? (spread / mid) * 100 : null,
        available: true,
      };
    }

    // Default: no prices available
    return { bid: null, mid: null, ask: null, spread: null, spreadPct: null, available: false };
  }, [ticker, type, action, legs, prices, option]);
}
