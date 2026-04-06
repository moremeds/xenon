/**
 * TDD — risk reversal chart mid-price fallback
 *
 * RED: these tests fail until `resolveChartPrice` is exported from
 *      usePriceHistory and implements the mid fallback logic.
 */

import { describe, it, expect } from "vitest";
import { resolveChartPrice } from "../lib/usePriceHistory";
import type { PriceData } from "../lib/pricesProtocol";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makePd(overrides: Partial<PriceData> = {}): PriceData {
  return {
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
    timestamp: "2026-03-09T12:00:00Z",
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// resolveChartPrice — unit tests
// ---------------------------------------------------------------------------

describe("resolveChartPrice — last-trade vs mid fallback", () => {
  it("returns last price when last is positive", () => {
    const pd = makePd({ last: 3.50, bid: 3.40, ask: 3.60 });
    const result = resolveChartPrice(pd);
    expect(result.price).toBe(3.50);
    expect(result.isMid).toBe(false);
  });

  it("returns null when price data is undefined", () => {
    const result = resolveChartPrice(undefined);
    expect(result.price).toBeNull();
    expect(result.isMid).toBe(false);
  });

  it("falls back to mid when last is null but bid/ask are available", () => {
    const pd = makePd({ last: null, bid: 2.80, ask: 3.20 });
    const result = resolveChartPrice(pd);
    expect(result.price).toBeCloseTo(3.00);
    expect(result.isMid).toBe(true);
  });

  it("falls back to mid when last is zero", () => {
    const pd = makePd({ last: 0, bid: 1.00, ask: 1.40 });
    const result = resolveChartPrice(pd);
    expect(result.price).toBeCloseTo(1.20);
    expect(result.isMid).toBe(true);
  });

  it("falls back to mid when last is negative", () => {
    const pd = makePd({ last: -1, bid: 5.00, ask: 5.20 });
    const result = resolveChartPrice(pd);
    expect(result.price).toBeCloseTo(5.10);
    expect(result.isMid).toBe(true);
  });

  it("returns null when last is null and only bid is available (no ask)", () => {
    const pd = makePd({ last: null, bid: 2.50, ask: null });
    const result = resolveChartPrice(pd);
    expect(result.price).toBeNull();
    expect(result.isMid).toBe(false);
  });

  it("returns null when last is null and only ask is available (no bid)", () => {
    const pd = makePd({ last: null, bid: null, ask: 2.50 });
    const result = resolveChartPrice(pd);
    expect(result.price).toBeNull();
    expect(result.isMid).toBe(false);
  });

  it("returns null when last, bid, and ask are all null", () => {
    const pd = makePd({ last: null, bid: null, ask: null });
    const result = resolveChartPrice(pd);
    expect(result.price).toBeNull();
    expect(result.isMid).toBe(false);
  });

  it("uses last price even when bid/ask are also available (last takes priority)", () => {
    const pd = makePd({ last: 4.75, bid: 4.50, ask: 5.00 });
    const result = resolveChartPrice(pd);
    expect(result.price).toBe(4.75);
    expect(result.isMid).toBe(false);
  });

  it("mid price is the arithmetic mean of bid and ask", () => {
    const bid = 1.23;
    const ask = 4.57;
    const pd = makePd({ last: null, bid, ask });
    const result = resolveChartPrice(pd);
    expect(result.price).toBeCloseTo((bid + ask) / 2);
    expect(result.isMid).toBe(true);
  });
});
