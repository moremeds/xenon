import type { PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";
import { legPriceKey, resolveSpreadPriceData } from "@/lib/positionUtils";

/**
 * Surface IB's calculated mark as a `last` fallback for illiquid single-leg
 * options. `market_price` on the leg is IB's model mark (sync writes it on
 * every /portfolio/sync); `market_price_is_calculated=true` means it is NOT a
 * live trade. Use it as `last` only when the live stream has no last of its
 * own — never overwrite a real last-trade tick.
 */
export function mergeCalculatedMark(
  priceData: PriceData | undefined,
  position: PortfolioPosition,
): PriceData | null {
  const leg = position.legs[0];
  const fallbackLast =
    leg?.market_price != null && leg.market_price > 0 ? leg.market_price : null;

  if (!priceData) {
    if (fallbackLast == null) return null;
    return {
      symbol: position.ticker,
      last: fallbackLast,
      lastIsCalculated: true,
      bid: null,
      ask: null,
      bidSize: null,
      askSize: null,
      volume: null,
      high: null,
      low: null,
      open: null,
      close: null,
      week52High: null,
      week52Low: null,
      avgVolume: null,
      delta: null,
      gamma: null,
      theta: null,
      vega: null,
      impliedVol: null,
      undPrice: null,
      timestamp: new Date().toISOString(),
    } as PriceData;
  }

  if (priceData.last != null && priceData.last > 0) return priceData;
  if (fallbackLast == null) return priceData;

  return { ...priceData, last: fallbackLast, lastIsCalculated: true };
}

export type TickerQuoteResolution = {
  priceData: PriceData | null;
  label?: string;
  priceKey?: string;
  isSpreadNet?: boolean;
};

/**
 * Resolve the best price data for the cockpit header / book.
 * - Stock positions → underlying ticker price
 * - Single-leg option → option contract price (bid/ask from WS), merged with
 *   the portfolio's calculated mark as a `last` fallback for illiquid contracts
 *   whose WS feed only delivers an ASK.
 * - Multi-leg → net spread price computed from per-leg WS bid/ask (a SIGNED
 *   net, credit negative / debit positive — flagged `isSpreadNet`). Falls back
 *   to underlying when leg prices are unavailable.
 * - No position → underlying ticker price
 */
export function resolveTickerQuote(
  ticker: string,
  position: PortfolioPosition | null,
  prices: Record<string, PriceData>,
): TickerQuoteResolution {
  if (!position || position.structure_type === "Stock") {
    return { priceData: prices[ticker] ?? null };
  }

  // Single-leg option: option-level prices, merged with the calculated mark.
  if (position.legs.length === 1) {
    const leg = position.legs[0];
    const key = legPriceKey(ticker, position.expiry, leg);
    if (key) {
      const merged = mergeCalculatedMark(prices[key], position);
      if (merged) {
        const strike = leg.strike ? `$${leg.strike}` : "";
        const type = leg.type === "Call" ? "C" : leg.type === "Put" ? "P" : "";
        return {
          priceData: merged,
          priceKey: key,
          label: `${ticker} ${position.expiry} ${strike} ${type}`,
        };
      }
    }
  }

  // Multi-leg: net spread price from per-leg WS prices (signed net).
  const spreadData = resolveSpreadPriceData(ticker, position, prices);
  if (spreadData) {
    return {
      priceData: spreadData,
      label: `${ticker} ${position.structure}`,
      isSpreadNet: true,
    };
  }

  // Fallback to underlying if leg prices unavailable.
  return { priceData: prices[ticker] ?? null, label: `${ticker} (underlying)` };
}
