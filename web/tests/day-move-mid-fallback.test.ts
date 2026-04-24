/**
 * TDD: computeDayMoveBreakdown — mid-price fallback when last is null/zero
 *
 * Bug: positions are excluded from the Day Move calculation when `last` is
 * null or zero, even if bid and ask are available. For illiquid options and
 * pre-market prices, IB may not have sent a last-trade tick yet.
 *
 * Fix: use (bid + ask) / 2 as fallback when last is unavailable.
 *
 * These tests FAIL before the fix (null last → position dropped).
 * They PASS after resolveLastOrMid() is wired in.
 */

import { describe, test, expect } from "vitest";
import {
  resolveLastOrMid,
  computeDayMoveBreakdown,
} from "../lib/dayMoveBreakdown";
import type { PriceData } from "../lib/pricesProtocol";
import type { PortfolioData } from "../lib/types";

// ── Helper to build a minimal PriceData ──────────────────────────────────────

const makePrice = (overrides: Partial<PriceData>): PriceData => ({
  symbol: "TEST",
  last: null,
  lastIsCalculated: false,
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
  timestamp: "2026-03-09T10:00:00Z",
  ...overrides,
});

// ── resolveLastOrMid unit tests ───────────────────────────────────────────────

describe("resolveLastOrMid", () => {
  test("returns last when it is positive", () => {
    const p = makePrice({ last: 42.50 });
    expect(resolveLastOrMid(p)).toBe(42.50);
  });

  test("last takes priority over mid when both are available", () => {
    const p = makePrice({ last: 42.50, bid: 10.00, ask: 10.20 });
    expect(resolveLastOrMid(p)).toBe(42.50);
  });

  test("falls back to mid when last is null and bid/ask are positive", () => {
    const p = makePrice({ last: null, bid: 10.00, ask: 10.20 });
    expect(resolveLastOrMid(p)).toBeCloseTo(10.10);
  });

  test("falls back to mid when last is zero", () => {
    const p = makePrice({ last: 0, bid: 0.50, ask: 0.60 });
    expect(resolveLastOrMid(p)).toBeCloseTo(0.55);
  });

  test("returns null when last is null and both bid/ask are null", () => {
    const p = makePrice({ last: null, bid: null, ask: null });
    expect(resolveLastOrMid(p)).toBeNull();
  });

  test("returns null when last is null and bid is null", () => {
    const p = makePrice({ last: null, bid: null, ask: 10.20 });
    expect(resolveLastOrMid(p)).toBeNull();
  });

  test("returns null when last is null and ask is null", () => {
    const p = makePrice({ last: null, bid: 10.00, ask: null });
    expect(resolveLastOrMid(p)).toBeNull();
  });

  test("returns null when last is null and bid is zero", () => {
    const p = makePrice({ last: null, bid: 0, ask: 0.60 });
    expect(resolveLastOrMid(p)).toBeNull();
  });

  test("returns null when last is null and ask is zero", () => {
    const p = makePrice({ last: null, bid: 0.50, ask: 0 });
    expect(resolveLastOrMid(p)).toBeNull();
  });
});

// ── Shared portfolio fixture helpers ─────────────────────────────────────────

const makePortfolio = (positions: PortfolioData["positions"]): PortfolioData => ({
  bankroll: 100_000,
  peak_value: 100_000,
  last_sync: "2026-03-09T10:00:00Z",
  positions,
  total_deployed_pct: 10,
  total_deployed_dollars: 10_000,
  remaining_capacity_pct: 90,
  position_count: positions.length,
  defined_risk_count: positions.length,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
});

const makeStockPosition = (overrides: Partial<PortfolioData["positions"][number]> = {}): PortfolioData["positions"][number] => ({
  id: 1,
  ticker: "AAPL",
  structure: "Long Stock",
  structure_type: "Stock",
  risk_profile: "undefined",
  expiry: "N/A",
  contracts: 100,
  direction: "LONG",
  entry_cost: 1000,
  max_risk: null,
  market_value: null,
  legs: [],
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "2026-01-01",
  ...overrides,
});

const makeOptionPosition = (overrides: Partial<PortfolioData["positions"][number]> = {}): PortfolioData["positions"][number] => ({
  id: 2,
  ticker: "AAPL",
  structure: "Long Call",
  structure_type: "Option",
  risk_profile: "defined",
  expiry: "2026-04-17",
  contracts: 1,
  direction: "LONG",
  entry_cost: 45,
  max_risk: 45,
  market_value: null,
  legs: [
    {
      direction: "LONG",
      contracts: 1,
      type: "Call",
      strike: 230,
      entry_cost: 45,
      avg_cost: 45,
      market_price: null,
      market_value: null,
    },
  ],
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "2026-01-01",
  ...overrides,
});

// ── computeDayMoveBreakdown: stock path with mid fallback ─────────────────────

describe("computeDayMoveBreakdown — stock mid fallback", () => {
  test("includes stock position when last=null but bid/ask available", () => {
    const pos = makeStockPosition({ ticker: "AAPL", contracts: 100 });
    const portfolio = makePortfolio([pos]);

    const prices: Record<string, PriceData> = {
      AAPL: makePrice({
        symbol: "AAPL",
        last: null,
        bid: 10.00,
        ask: 10.20,
        close: 9.80,
      }),
    };

    const { rows, total } = computeDayMoveBreakdown(portfolio, prices);
    expect(rows).toHaveLength(1);

    // mid = (10.00 + 10.20) / 2 = 10.10; pnl = (10.10 - 9.80) * 100 = 30
    expect(total).toBeCloseTo(30);
    expect(rows[0].col2).toContain("10.10");
  });

  test("excludes stock position when last=null and bid/ask also null", () => {
    const pos = makeStockPosition({ ticker: "AAPL", contracts: 100 });
    const portfolio = makePortfolio([pos]);

    const prices: Record<string, PriceData> = {
      AAPL: makePrice({ symbol: "AAPL", last: null, bid: null, ask: null, close: 9.80 }),
    };

    const { rows } = computeDayMoveBreakdown(portfolio, prices);
    expect(rows).toHaveLength(0);
  });

  test("uses last price when both last and mid are available (stock)", () => {
    const pos = makeStockPosition({ ticker: "AAPL", contracts: 100 });
    const portfolio = makePortfolio([pos]);

    const prices: Record<string, PriceData> = {
      AAPL: makePrice({
        symbol: "AAPL",
        last: 11.00,
        bid: 10.00,
        ask: 10.20,
        close: 9.80,
      }),
    };

    const { rows, total } = computeDayMoveBreakdown(portfolio, prices);
    expect(rows).toHaveLength(1);

    // Should use last=11.00, not mid=10.10; pnl = (11.00 - 9.80) * 100 = 120
    expect(total).toBeCloseTo(120);
    expect(rows[0].col2).toContain("11.00");
    expect(rows[0].col2).not.toContain("MID");
  });

  test("prefers IB per-position daily P&L over close-based stock math when present", () => {
    const pos = makeStockPosition({
      ticker: "AAPL",
      contracts: 100,
      ib_daily_pnl: -250,
    });
    const portfolio = makePortfolio([pos]);

    const prices: Record<string, PriceData> = {
      AAPL: makePrice({
        symbol: "AAPL",
        last: 11.0,
        bid: 10.9,
        ask: 11.1,
        close: 10.0,
      }),
    };

    const { rows, total } = computeDayMoveBreakdown(portfolio, prices);
    expect(rows).toHaveLength(1);

    // Close-based math would be +100.00, but same-day adds must use IB dailyPnL.
    expect(total).toBeCloseTo(-250);
    expect(rows[0].pnl).toBeCloseTo(-250);
    expect(rows[0].col1).toContain("10.00");
    expect(rows[0].col2).toContain("11.00");
  });
});

// ── computeDayMoveBreakdown: option/leg path with mid fallback ────────────────

describe("computeDayMoveBreakdown — option mid fallback", () => {
  test("includes option position when last=null but bid/ask available", () => {
    const pos = makeOptionPosition({ ticker: "AAPL", contracts: 1 });
    const portfolio = makePortfolio([pos]);

    // Key format: AAPL_20260417_230_C
    const optionKey = "AAPL_20260417_230_C";
    const prices: Record<string, PriceData> = {
      [optionKey]: makePrice({
        symbol: optionKey,
        last: null,
        bid: 0.50,
        ask: 0.60,
        close: 0.45,
      }),
    };

    const { rows, total } = computeDayMoveBreakdown(portfolio, prices);
    expect(rows).toHaveLength(1);

    // mid = (0.50 + 0.60) / 2 = 0.55; pnl = +1 * (0.55 - 0.45) * 1 * 100 = 10
    expect(total).toBeCloseTo(10);
    expect(rows[0].col2).toContain("0.55");
  });

  test("excludes option position when last=null and bid/ask also null", () => {
    const pos = makeOptionPosition({ ticker: "AAPL", contracts: 1 });
    const portfolio = makePortfolio([pos]);

    const optionKey = "AAPL_20260417_230_C";
    const prices: Record<string, PriceData> = {
      [optionKey]: makePrice({
        symbol: optionKey,
        last: null,
        bid: null,
        ask: null,
        close: 0.45,
      }),
    };

    const { rows } = computeDayMoveBreakdown(portfolio, prices);
    expect(rows).toHaveLength(0);
  });

  test("uses last price for options when spread is narrow and last is inside", () => {
    const pos = makeOptionPosition({ ticker: "AAPL", contracts: 1 });
    const portfolio = makePortfolio([pos]);

    const optionKey = "AAPL_20260417_230_C";
    // Narrow spread (4%) — last should be used as-is
    const prices: Record<string, PriceData> = {
      [optionKey]: makePrice({
        symbol: optionKey,
        last: 0.58,
        bid: 0.56,
        ask: 0.60,
        close: 0.45,
      }),
    };

    const { rows, total } = computeDayMoveBreakdown(portfolio, prices);
    expect(rows).toHaveLength(1);

    // last=0.58 inside narrow spread, so last is used
    expect(total).toBeCloseTo(13);
    expect(rows[0].col2).toContain("0.58");
  });

  test("uses midpoint when an option last price is stale and far outside bid/ask", () => {
    const pos = makeOptionPosition({
      ticker: "WULF",
      structure: "Long Call",
      structure_type: "Long Call",
      expiry: "2027-01-15",
      contracts: 77,
      legs: [
        {
          direction: "LONG",
          contracts: 77,
          type: "Call",
          strike: 17,
          entry_cost: 40076.51,
          avg_cost: 520.4741844,
          market_price: 4.475,
          market_value: 34457.5,
          market_price_is_calculated: false,
        },
      ],
    });
    const portfolio = makePortfolio([pos]);

    const optionKey = "WULF_20270115_17_C";
    const prices: Record<string, PriceData> = {
      [optionKey]: makePrice({
        symbol: optionKey,
        last: 21.015,
        bid: 4.20,
        ask: 4.75,
        close: 4.78,
      }),
    };

    const { rows, total } = computeDayMoveBreakdown(portfolio, prices);
    expect(rows).toHaveLength(1);

    // Mid = 4.475, so the day move should use a small negative change, not a massive positive one.
    expect(total).toBeCloseTo((4.475 - 4.78) * 77 * 100, 2);
    expect(rows[0].col2).toContain("4.47");
    expect(rows[0].col2).toContain("MID");
  });

  test("prefers IB per-position daily P&L over close-based option math when present", () => {
    const pos = makeOptionPosition({
      ticker: "WULF",
      structure: "Long Call",
      structure_type: "Long Call",
      expiry: "2027-01-15",
      contracts: 77,
      ib_daily_pnl: -3688.02,
      legs: [
        {
          direction: "LONG",
          contracts: 77,
          type: "Call",
          strike: 17,
          entry_cost: 40076.51,
          avg_cost: 520.4741844,
          market_price: 4.5,
          market_value: 34650,
        },
      ],
    });
    const portfolio = makePortfolio([pos]);

    const optionKey = "WULF_20270115_17_C";
    const prices: Record<string, PriceData> = {
      [optionKey]: makePrice({
        symbol: optionKey,
        last: 4.5,
        bid: 4.45,
        ask: 4.55,
        close: 4.41,
      }),
    };

    const { rows, total } = computeDayMoveBreakdown(portfolio, prices);
    expect(rows).toHaveLength(1);

    // Close-based math would be +693.00, but same-day adds must use IB dailyPnL.
    expect(total).toBeCloseTo(-3688.02);
    expect(rows[0].pnl).toBeCloseTo(-3688.02);
    expect(rows[0].col1).toContain("4.41");
    expect(rows[0].col2).toContain("4.50");
  });
});
