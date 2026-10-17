import { describe, it, expect } from "vitest";
import { optionKey } from "../lib/pricesProtocol";
import type { PriceData } from "../lib/pricesProtocol";
import type { PortfolioPosition } from "../lib/types";
import { getOptionDailyChg } from "../lib/positionUtils";

/**
 * Real test of getOptionDailyChg (web/lib/positionUtils.ts) — the non-same-day
 * branch. Day Chg % = daily P&L / |yesterday's close value| × 100, NEVER
 * divided by entry cost. (Same-day coverage lives in same-day-pnl.test.ts.)
 * Replaces the lib/tools/__tests__/daily-chg.test.ts mirror, which tested a
 * re-implemented copy instead of the production function.
 */

function makePriceData(overrides: Partial<PriceData> = {}): PriceData {
  return {
    symbol: "TEST", last: null, lastIsCalculated: false,
    bid: null, ask: null, bidSize: null, askSize: null,
    volume: null, high: null, low: null, open: null, close: null,
    delta: null, gamma: null, theta: null, vega: null, impliedVol: null, undPrice: null,
    timestamp: new Date().toISOString(),
    ...overrides,
  };
}

// PLTR bull call spread, entered 2026-03-05 (NOT today → close-value branch).
// entry_cost $2,600 is deliberately tiny so a buggy entry-cost denominator
// would produce an absurd ~30% move instead of ~1.9%.
const pltrSpread: PortfolioPosition = {
  id: 1,
  ticker: "PLTR",
  structure: "Bull Call Spread $145.0/$165.0",
  structure_type: "Vertical Spread",
  risk_profile: "defined",
  expiry: "2026-03-27",
  contracts: 50,
  direction: "DEBIT",
  entry_cost: 2600,
  max_risk: 2600,
  market_value: null,
  ib_daily_pnl: null,
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "2026-03-05",
  legs: [
    { direction: "LONG", contracts: 50, type: "Call", strike: 145, entry_cost: 22950, avg_cost: 22950, market_price: null, market_value: null },
    { direction: "SHORT", contracts: 50, type: "Call", strike: 165, entry_cost: 20350, avg_cost: 20350, market_price: null, market_value: null },
  ],
};

const longKey = optionKey({ symbol: "PLTR", expiry: "20260327", strike: 145, right: "C" });
const shortKey = optionKey({ symbol: "PLTR", expiry: "20260327", strike: 165, right: "C" });

const pltrPrices: Record<string, PriceData> = {
  // Net close = (12.00 - 3.50) * 50 * 100 = $42,500
  // Net last  = (11.50 - 2.84) * 50 * 100 = $43,300 → daily P&L $800
  [longKey]: makePriceData({ symbol: longKey, last: 11.5, close: 12.0 }),
  [shortKey]: makePriceData({ symbol: shortKey, last: 2.84, close: 3.5 }),
};

describe("getOptionDailyChg — non-same-day (close-value denominator)", () => {
  it("computes % of yesterday's close value, not entry cost", () => {
    const result = getOptionDailyChg(pltrSpread, pltrPrices);
    // $800 / $42,500 = +1.88%. Entry-cost denominator would give ~30.8%.
    expect(result).not.toBeNull();
    expect(result!).toBeCloseTo(1.882, 1);
    expect(Math.abs(result!)).toBeLessThan(30);
  });

  it("prefers ib_daily_pnl over the WS close-based computation", () => {
    const pos = { ...pltrSpread, ib_daily_pnl: 425 };
    // 425 / 42,500 = +1.0% — proves IB's number wins over wsDailyPnl ($800).
    expect(getOptionDailyChg(pos, pltrPrices)).toBeCloseTo(1.0, 3);
  });

  it("single-leg option: negative daily change is % of close", () => {
    const pos: PortfolioPosition = {
      id: 4, ticker: "AAOI", structure: "Long Call $105",
      structure_type: "Option", risk_profile: "defined",
      expiry: "2026-03-06", contracts: 25, direction: "LONG",
      entry_cost: 6621, max_risk: 6621, market_value: null,
      ib_daily_pnl: null, kelly_optimal: null, target: null, stop: null,
      entry_date: "2026-03-05",
      legs: [{ direction: "LONG", contracts: 25, type: "Call", strike: 105, entry_cost: 6621, avg_cost: 6621, market_price: null, market_value: null }],
    };
    const key = optionKey({ symbol: "AAOI", expiry: "20260306", strike: 105, right: "C" });
    const prices = { [key]: makePriceData({ symbol: key, last: 1.4, close: 2.0 }) };
    // dailyPnl = (1.40 - 2.00) * 25 * 100 = -$1,500; closeValue = $5,000 → -30%
    expect(getOptionDailyChg(pos, prices)).toBeCloseTo(-30, 1);
  });

  it("returns null for stock positions", () => {
    const pos: PortfolioPosition = {
      id: 2, ticker: "AAPL", structure: "Long Stock",
      structure_type: "Stock", risk_profile: "equity", expiry: "N/A",
      contracts: 100, direction: "LONG", entry_cost: 15000, max_risk: null,
      market_value: null, ib_daily_pnl: null, kelly_optimal: null, target: null,
      stop: null, entry_date: "2026-01-01",
      legs: [{ direction: "LONG", contracts: 100, type: "Stock", strike: null, entry_cost: 15000, avg_cost: 15000, market_price: null, market_value: null }],
    };
    expect(getOptionDailyChg(pos, {})).toBeNull();
    expect(getOptionDailyChg(pos, pltrPrices)).toBeNull();
  });

  it("returns null when no leg has a positive close", () => {
    const prices: Record<string, PriceData> = {
      [longKey]: makePriceData({ symbol: longKey, last: 11.5, close: null }),
      [shortKey]: makePriceData({ symbol: shortKey, last: 2.84, close: null }),
    };
    expect(getOptionDailyChg(pltrSpread, prices)).toBeNull();
  });

  it("returns null when net close value is zero", () => {
    const prices: Record<string, PriceData> = {
      [longKey]: makePriceData({ symbol: longKey, last: 5.0, close: 3.0 }),
      [shortKey]: makePriceData({ symbol: shortKey, last: 4.0, close: 3.0 }),
    };
    // closeValue = (3.00 - 3.00) * 50 * 100 = 0
    expect(getOptionDailyChg(pltrSpread, prices)).toBeNull();
  });

  it("returns null when a leg quote is missing entirely", () => {
    const prices: Record<string, PriceData> = {
      [longKey]: makePriceData({ symbol: longKey, last: 11.5, close: 12.0 }),
    };
    expect(getOptionDailyChg(pltrSpread, prices)).toBeNull();
  });

  it("returns null without a prices map", () => {
    expect(getOptionDailyChg(pltrSpread)).toBeNull();
  });
});
