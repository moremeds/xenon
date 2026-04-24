import { describe, it, expect } from "vitest";
import type { PortfolioPosition } from "../lib/types";
import { normalizeSymbolList, symbolKey } from "../lib/pricesProtocol";
import { fmtPriceOrCalculated, getLastPriceIsCalculated, resolveRealtimePrice } from "../lib/positionUtils";
import type { PriceData } from "../lib/pricesProtocol";

/**
 * Tests for real-time price functionality.
 *
 * These tests verify:
 * 1. Price protocol helpers (normalizeSymbolList, symbolKey)
 * 2. Price formatting (fmtPriceOrCalculated)
 * 3. Portfolio calculation flags (getLastPriceIsCalculated)
 */

// =============================================================================
// Price Protocol Helpers
// =============================================================================

describe("Price protocol helpers", () => {
  it("normalizes symbols with trim/uppercase/filter", () => {
    expect(
      normalizeSymbolList([" aapl ", "", "  ", "MsFt", "NVDA"]),
    ).toEqual(["AAPL", "MSFT", "NVDA"]);
  });

  it("builds stable symbol keys", () => {
    const first = symbolKey(["MSFT", "AAPL", "NVDA"]);
    const second = symbolKey(["NVDA", "AAPL", "MSFT"]);
    expect(first).toBe(second);
    expect(first).toBe("AAPL,MSFT,NVDA");
  });
});

// =============================================================================
// Price Formatting (fmtPriceOrCalculated)
// =============================================================================

describe("Price formatting utilities", () => {
  it("prefixes C for calculated prices", () => {
    expect(fmtPriceOrCalculated(175.5, true)).toBe("C$175.50");
  });

  it("does not prefix C for raw prices", () => {
    expect(fmtPriceOrCalculated(175.5, false)).toBe("$175.50");
  });
});

describe("Realtime price resolution", () => {
  const makePriceData = (overrides: Partial<PriceData>): PriceData => ({
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
    timestamp: "2026-03-19T13:36:50.472Z",
    ...overrides,
  });

  it("uses bid/ask midpoint for option quotes when last is far outside the live market", () => {
    const resolved = resolveRealtimePrice(
      makePriceData({
        symbol: "WULF_20270115_17_C",
        last: 21.015,
        bid: 4.2,
        ask: 4.75,
        close: 4.78,
      }),
    );

    expect(resolved.price).toBeCloseTo(4.475, 3);
    expect(resolved.isCalculated).toBe(true);
  });

  it("keeps stock last prices even when they sit outside the current spread", () => {
    const resolved = resolveRealtimePrice(
      makePriceData({
        symbol: "WULF",
        last: 14.73,
        bid: 14.2,
        ask: 14.25,
        close: 14.4,
      }),
    );

    expect(resolved.price).toBe(14.73);
    expect(resolved.isCalculated).toBe(false);
  });
});

// =============================================================================
// Portfolio Calculation Flags
// =============================================================================

describe("Portfolio calculation flags", () => {
  const buildLeg = (
    marketPriceIsCalculated?: boolean
  ): PortfolioPosition["legs"][number] => ({
    direction: "LONG",
    contracts: 1,
    type: "Stock",
    strike: null,
    entry_cost: 100,
    avg_cost: 100,
    market_price: 100,
    market_value: 100,
    ...(marketPriceIsCalculated == null ? {} : { market_price_is_calculated: marketPriceIsCalculated }),
  });

  const makePosition = (
    marketPriceIsCalculated: boolean | undefined,
    legs: PortfolioPosition["legs"]
  ): PortfolioPosition => ({
    id: 1,
    ticker: "TEST",
    structure: "Stock",
    structure_type: "Stock",
    risk_profile: "equity",
    expiry: "N/A",
    contracts: 1,
    direction: "LONG",
    entry_cost: 100,
    max_risk: null,
    market_value: 100,
    legs,
    market_price_is_calculated: marketPriceIsCalculated,
    kelly_optimal: null,
    target: null,
    stop: null,
    entry_date: "2026-01-01T00:00:00Z",
  });

  it("prefers explicit position-level calculated flag", () => {
    const position = makePosition(false, [buildLeg(true)]);
    expect(getLastPriceIsCalculated(position)).toBe(false);
  });

  it("falls back to leg-level flags when position-level flag is missing", () => {
    const position = makePosition(undefined, [buildLeg(true)]);
    expect(getLastPriceIsCalculated(position)).toBe(true);
  });

  it("returns false when no flags are present", () => {
    const position = makePosition(undefined, [buildLeg(undefined)]);
    expect(getLastPriceIsCalculated(position)).toBe(false);
  });
});
