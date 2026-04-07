/**
 * Unit tests: getDisplayMarketValue / getDisplayTotalPnl.
 *
 * These helpers must match what `PositionTable`'s PositionRow renders inline,
 * so aggregated header values (Portfolio By-Structure card totals) stay in
 * lockstep with the per-row values beneath them.
 */

import { describe, it, expect } from "vitest";
import { getDisplayMarketValue, getDisplayTotalPnl } from "@/lib/positionUtils";
import type { PortfolioPosition } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";

function mkPrice(overrides: Partial<PriceData> = {}): PriceData {
  return { last: null, bid: null, ask: null, close: null, volume: null, ...overrides };
}

const STOCK: PortfolioPosition = {
  id: 1,
  ticker: "MSFT",
  structure: "Stock (1000 shares)",
  structure_type: "Stock",
  risk_profile: "equity",
  direction: "LONG",
  contracts: 1000,
  expiry: "N/A",
  entry_cost: 468_000,
  market_value: 400_000,
  legs: [
    { type: "Stock", direction: "LONG", strike: null, contracts: 1000, avg_cost: 468, entry_cost: 468_000, market_price: null, market_value: 400_000 },
  ],
};

const SPREAD: PortfolioPosition = {
  id: 2,
  ticker: "AAPL",
  structure: "Bull Call Spread $270.0/$290.0",
  structure_type: "Vertical",
  risk_profile: "defined",
  direction: "DEBIT",
  contracts: 100,
  expiry: "2026-04-17",
  entry_cost: 40_100,
  market_value: 41_700,
  legs: [
    { type: "Call", direction: "LONG", strike: 270, contracts: 100, avg_cost: 901, entry_cost: 90_100, market_price: null, market_value: 63_000 },
    { type: "Call", direction: "SHORT", strike: 290, contracts: 100, avg_cost: -500, entry_cost: -50_000, market_price: null, market_value: -21_300 },
  ],
};

describe("getDisplayMarketValue", () => {
  it("stock: uses live last * contracts when WS price is present", () => {
    const prices = { MSFT: mkPrice({ last: 410 }) };
    expect(getDisplayMarketValue(STOCK, prices)).toBe(410 * 1000);
  });

  it("stock: falls back to resolveMarketValue when no live last", () => {
    expect(getDisplayMarketValue(STOCK, {})).toBe(400_000);
  });

  it("option spread: uses realtime leg prices (sign-aware sum)", () => {
    const prices = {
      AAPL_20260417_270_C: mkPrice({ last: 6.50 }),
      AAPL_20260417_290_C: mkPrice({ last: 2.00 }),
    };
    // LONG 6.50 * 100 * 100 + SHORT -2.00 * 100 * 100 = 65000 - 20000 = 45000
    expect(getDisplayMarketValue(SPREAD, prices)).toBe(45_000);
  });

  it("option spread: falls back to resolveMarketValue when any leg missing", () => {
    const prices = { AAPL_20260417_270_C: mkPrice({ last: 6.50 }) };
    // Short leg missing → fall back to aggregate market_value from legs
    // resolveMarketValue (multi-leg path): LONG 63000 + SHORT -21300 = 41700
    expect(getDisplayMarketValue(SPREAD, prices)).toBe(41_700);
  });

  it("returns null when no source is available", () => {
    const noData: PortfolioPosition = { ...STOCK, market_value: null, legs: [{ ...STOCK.legs[0], market_value: null }] };
    expect(getDisplayMarketValue(noData, {})).toBe(null);
  });
});

describe("getDisplayTotalPnl", () => {
  it("preserves sign: debit spread with MV > EC is positive", () => {
    const prices = {
      AAPL_20260417_270_C: mkPrice({ last: 6.50 }),
      AAPL_20260417_290_C: mkPrice({ last: 2.00 }),
    };
    // MV 45000 - EC (901*100 - 500*100 = 40100) = +4900
    expect(getDisplayTotalPnl(SPREAD, prices)).toBe(4_900);
  });

  it("preserves sign: credit spread (negative EC) computes correctly without Math.abs", () => {
    // Short Put: entry_cost negative (collected premium); MV less negative → positive P&L
    const shortPut: PortfolioPosition = {
      id: 3,
      ticker: "TSLA",
      structure: "Short Put $440.0",
      structure_type: "Short Put",
      risk_profile: "undefined",
      direction: "CREDIT",
      contracts: 1,
      expiry: "2026-04-17",
      entry_cost: -500, // collected $5 * 100
      market_value: -200,
      legs: [
        { type: "Put", direction: "SHORT", strike: 440, contracts: 1, avg_cost: -500, entry_cost: -500, market_price: null, market_value: -200 },
      ],
    };
    // With no price data: MV = resolveMarketValue = single-leg market_value = -200 (from pos.market_value)
    //   resolveEntryCost single-leg = pos.entry_cost = -500
    //   pnl = -200 - (-500) = +300
    expect(getDisplayTotalPnl(shortPut, {})).toBe(300);
  });

  it("returns null when MV cannot be resolved", () => {
    const noData: PortfolioPosition = { ...STOCK, market_value: null, legs: [{ ...STOCK.legs[0], market_value: null }] };
    expect(getDisplayTotalPnl(noData, {})).toBe(null);
  });
});
