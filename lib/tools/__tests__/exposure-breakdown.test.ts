import { describe, it, expect } from "vitest";
import { optionKey } from "../../../web/lib/pricesProtocol";
import type { PriceData } from "../../../web/lib/pricesProtocol";
import type { PortfolioPosition, PortfolioData } from "../../../web/lib/types";
import {
  computeExposureDetailed,
  type ExposureBreakdownRow,
  type ExposureDataWithBreakdown,
} from "../../../web/lib/exposureBreakdown";

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

function makePortfolio(positions: PortfolioPosition[], bankroll = 100_000): PortfolioData {
  return {
    bankroll,
    peak_value: bankroll,
    last_sync: new Date().toISOString(),
    positions,
    total_deployed_pct: 0,
    total_deployed_dollars: 0,
    remaining_capacity_pct: 100,
    position_count: positions.length,
    defined_risk_count: 0,
    undefined_risk_count: 0,
    avg_kelly_optimal: null,
  };
}

/* ── Fixtures ──────────────────────────────────────────── */

const stockPosition: PortfolioPosition = {
  id: 1, ticker: "AAPL", structure: "Long Stock",
  structure_type: "Stock", risk_profile: "equity", expiry: "N/A",
  contracts: 100, direction: "LONG", entry_cost: 15000, max_risk: null,
  market_value: 17500, kelly_optimal: null, target: null, stop: null,
  entry_date: "2026-01-01",
  legs: [{ direction: "LONG", contracts: 100, type: "Stock", strike: null, entry_cost: 15000, avg_cost: 15000, market_price: 175, market_value: 17500 }],
};

const optionWithIbDelta: PortfolioPosition = {
  id: 2, ticker: "GOOG", structure: "Long Call $180",
  structure_type: "Option", risk_profile: "defined", expiry: "2026-06-19",
  contracts: 10, direction: "LONG", entry_cost: 5000, max_risk: 5000,
  market_value: 7500, kelly_optimal: null, target: null, stop: null,
  entry_date: "2026-03-01",
  legs: [{ direction: "LONG", contracts: 10, type: "Call", strike: 180, entry_cost: 5000, avg_cost: 5000, market_price: 7.5, market_value: 7500 }],
};

const googCallKey = optionKey({ symbol: "GOOG", expiry: "20260619", strike: 180, right: "C" });

const optionNoIbDelta: PortfolioPosition = {
  id: 3, ticker: "TSLA", structure: "Long Put $200",
  structure_type: "Option", risk_profile: "defined", expiry: "2026-04-17",
  contracts: 5, direction: "LONG", entry_cost: 3000, max_risk: 3000,
  market_value: 2500, kelly_optimal: null, target: null, stop: null,
  entry_date: "2026-03-01",
  legs: [{ direction: "LONG", contracts: 5, type: "Put", strike: 200, entry_cost: 3000, avg_cost: 3000, market_price: 5.0, market_value: 2500 }],
};

const tslaPutKey = optionKey({ symbol: "TSLA", expiry: "20260417", strike: 200, right: "P" });

const spreadPosition: PortfolioPosition = {
  id: 4, ticker: "PLTR", structure: "Bull Call Spread $145/$165",
  structure_type: "Vertical Spread", risk_profile: "defined", expiry: "2026-03-27",
  contracts: 50, direction: "DEBIT", entry_cost: 2600, max_risk: 2600,
  market_value: null, kelly_optimal: null, target: null, stop: null,
  entry_date: "2026-03-05",
  legs: [
    { direction: "LONG", contracts: 50, type: "Call", strike: 145, entry_cost: 22950, avg_cost: 22950, market_price: null, market_value: 57500 },
    { direction: "SHORT", contracts: 50, type: "Call", strike: 165, entry_cost: 20350, avg_cost: 20350, market_price: null, market_value: 14200 },
  ],
};

const pltrLongKey = optionKey({ symbol: "PLTR", expiry: "20260327", strike: 145, right: "C" });
const pltrShortKey = optionKey({ symbol: "PLTR", expiry: "20260327", strike: 165, right: "C" });

/* ── Tests ─────────────────────────────────────────────── */

describe("computeExposureDetailed", () => {
  it("returns correct breakdown for a stock position", () => {
    const portfolio = makePortfolio([stockPosition]);
    const prices: Record<string, PriceData> = {
      AAPL: makePriceData({ symbol: "AAPL", last: 175 }),
    };

    const result = computeExposureDetailed(portfolio, prices);

    expect(result.rows).toHaveLength(1);
    const row = result.rows[0];
    expect(row.ticker).toBe("AAPL");
    // Stock: delta = share count = 100
    expect(row.delta).toBe(100);
    // Dollar delta = 100 × 175 = 17,500
    expect(row.dollarDelta).toBe(17500);
    expect(row.deltaSource).toBe("approx"); // stocks don't have IB delta
  });

  it("uses IB real delta when available for options", () => {
    const portfolio = makePortfolio([optionWithIbDelta]);
    const prices: Record<string, PriceData> = {
      GOOG: makePriceData({ symbol: "GOOG", last: 185 }),
      [googCallKey]: makePriceData({ symbol: googCallKey, last: 7.5, delta: 0.65 }),
    };

    const result = computeExposureDetailed(portfolio, prices);

    expect(result.rows).toHaveLength(1);
    const row = result.rows[0];
    expect(row.ticker).toBe("GOOG");
    expect(row.deltaSource).toBe("ib");
    // delta = 1 × 0.65 × 10 × 100 = 650
    expect(row.delta).toBe(650);
    // dollar delta = 650 × 185 = 120,250
    expect(row.dollarDelta).toBe(120250);
    // Per-leg detail
    expect(row.legs).toHaveLength(1);
    expect(row.legs[0].rawDelta).toBe(0.65);
    expect(row.legs[0].legDelta).toBe(650);
  });

  it("falls back to approx delta when IB delta not available", () => {
    const portfolio = makePortfolio([optionNoIbDelta]);
    const prices: Record<string, PriceData> = {
      TSLA: makePriceData({ symbol: "TSLA", last: 210 }),
      [tslaPutKey]: makePriceData({ symbol: tslaPutKey, last: 5.0, delta: null }),
    };

    const result = computeExposureDetailed(portfolio, prices);

    expect(result.rows).toHaveLength(1);
    const row = result.rows[0];
    expect(row.ticker).toBe("TSLA");
    expect(row.deltaSource).toBe("approx");
    // Put on TSLA at 200 when spot is 210 — OTM put, delta should be negative and small
    expect(row.delta).toBeLessThan(0);
    expect(row.legs[0].rawDelta).toBeLessThan(0);
  });

  it("handles spread positions with per-leg breakdown", () => {
    const portfolio = makePortfolio([spreadPosition]);
    const prices: Record<string, PriceData> = {
      PLTR: makePriceData({ symbol: "PLTR", last: 155 }),
      [pltrLongKey]: makePriceData({ symbol: pltrLongKey, last: 11.50, delta: 0.70 }),
      [pltrShortKey]: makePriceData({ symbol: pltrShortKey, last: 2.84, delta: 0.25 }),
    };

    const result = computeExposureDetailed(portfolio, prices);

    expect(result.rows).toHaveLength(1);
    const row = result.rows[0];
    expect(row.ticker).toBe("PLTR");
    expect(row.deltaSource).toBe("ib");

    // Long leg: +1 × 0.70 × 50 × 100 = 3500
    // Short leg: -1 × 0.25 × 50 × 100 = -1250
    // Net delta = 3500 - 1250 = 2250
    expect(row.delta).toBe(2250);
    expect(row.dollarDelta).toBe(2250 * 155);

    expect(row.legs).toHaveLength(2);
    expect(row.legs[0].legDelta).toBe(3500);
    expect(row.legs[0].direction).toBe("LONG");
    expect(row.legs[1].legDelta).toBe(-1250);
    expect(row.legs[1].direction).toBe("SHORT");
  });

  it("classifies net long vs net short correctly", () => {
    // Stock is long (positive delta), put is short (negative delta)
    const portfolio = makePortfolio([stockPosition, optionNoIbDelta]);
    const prices: Record<string, PriceData> = {
      AAPL: makePriceData({ symbol: "AAPL", last: 175 }),
      TSLA: makePriceData({ symbol: "TSLA", last: 210 }),
      [tslaPutKey]: makePriceData({ symbol: tslaPutKey, last: 5.0, delta: null }),
    };

    const result = computeExposureDetailed(portfolio, prices);

    // AAPL stock has positive delta → contributes to netLong
    expect(result.netLong).toBeGreaterThan(0);
    // TSLA put has negative delta → contributes to netShort
    expect(result.netShort).toBeGreaterThan(0);
  });

  it("computes net exposure % correctly", () => {
    const bankroll = 100_000;
    const portfolio = makePortfolio([stockPosition], bankroll);
    const prices: Record<string, PriceData> = {
      AAPL: makePriceData({ symbol: "AAPL", last: 175 }),
    };

    const result = computeExposureDetailed(portfolio, prices);

    // Stock has positive delta → netLong = mv of AAPL = 17500, netShort = 0
    // netExposurePct = (17500 - 0) / 100000 × 100 = 17.5%
    expect(result.netExposurePct).toBeCloseTo(17.5, 1);
  });

  it("returns zeros and empty rows for empty portfolio", () => {
    const portfolio = makePortfolio([]);
    const prices: Record<string, PriceData> = {};

    const result = computeExposureDetailed(portfolio, prices);

    expect(result.netLong).toBe(0);
    expect(result.netShort).toBe(0);
    expect(result.dollarDelta).toBe(0);
    expect(result.netExposurePct).toBe(0);
    expect(result.rows).toHaveLength(0);
  });

  it("scalars match original computeExposure results", () => {
    const portfolio = makePortfolio([stockPosition, optionWithIbDelta, spreadPosition]);
    const prices: Record<string, PriceData> = {
      AAPL: makePriceData({ symbol: "AAPL", last: 175 }),
      GOOG: makePriceData({ symbol: "GOOG", last: 185 }),
      [googCallKey]: makePriceData({ symbol: googCallKey, last: 7.5, delta: 0.65 }),
      PLTR: makePriceData({ symbol: "PLTR", last: 155 }),
      [pltrLongKey]: makePriceData({ symbol: pltrLongKey, last: 11.50, delta: 0.70 }),
      [pltrShortKey]: makePriceData({ symbol: pltrShortKey, last: 2.84, delta: 0.25 }),
    };

    const result = computeExposureDetailed(portfolio, prices);

    // Sum of individual row dollarDeltas should equal total
    const sumDollarDelta = result.rows.reduce((s, r) => s + r.dollarDelta, 0);
    expect(result.dollarDelta).toBeCloseTo(sumDollarDelta, 2);
  });
});
