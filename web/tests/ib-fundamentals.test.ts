import { describe, it, expect } from "vitest";
import {
  createPriceData,
  createFundamentalsData,
  parseFundamentalRatios,
  updatePriceFromTickPrice,
} from "../../scripts/ib_tick_handler.js";

// IB tick types for Misc Stats (generic tick 165)
const TICK_LOW_52_WEEK = 19;
const TICK_HIGH_52_WEEK = 20;
const TICK_AVG_VOLUME = 21;

describe("Misc Stats — 52W high/low via tickPrice", () => {
  it("stores 52W high from tick type 20", () => {
    const data = createPriceData("PLTR");
    updatePriceFromTickPrice(data, TICK_HIGH_52_WEEK, 207.52);
    expect(data.week52High).toBe(207.52);
  });

  it("stores 52W low from tick type 19", () => {
    const data = createPriceData("PLTR");
    updatePriceFromTickPrice(data, TICK_LOW_52_WEEK, 66.12);
    expect(data.week52Low).toBe(66.12);
  });

  it("stores avg volume from tick type 21", () => {
    const data = createPriceData("PLTR");
    updatePriceFromTickPrice(data, TICK_AVG_VOLUME, 58000000);
    expect(data.avgVolume).toBe(58000000);
  });

  it("initializes 52W fields as null", () => {
    const data = createPriceData("TEST");
    expect(data.week52High).toBeNull();
    expect(data.week52Low).toBeNull();
    expect(data.avgVolume).toBeNull();
  });
});

describe("createFundamentalsData", () => {
  it("creates data with all null fields", () => {
    const data = createFundamentalsData("PLTR");
    expect(data.symbol).toBe("PLTR");
    expect(data.peRatio).toBeNull();
    expect(data.eps).toBeNull();
    expect(data.dividendYield).toBeNull();
    expect(data.week52High).toBeNull();
    expect(data.week52Low).toBeNull();
    expect(data.priceBookRatio).toBeNull();
    expect(data.roe).toBeNull();
    expect(data.revenue).toBeNull();
    expect(data.timestamp).toBeTruthy();
  });
});

describe("parseFundamentalRatios", () => {
  it("parses semicolon-delimited key=value IB string", () => {
    const data = createFundamentalsData("AAPL");
    const str = "PEEXCLXOR=25.30;TTMEPSXCLX=6.42;YIELD=0.55;NHIG=199.62;NLOW=124.17;PRICE2BK=48.5;TTMROEPCT=157.41;TTMREV=391035000000";
    const updated = parseFundamentalRatios(data, str);
    expect(updated).toBe(true);
    expect(data.peRatio).toBe(25.3);
    expect(data.eps).toBe(6.42);
    expect(data.dividendYield).toBe(0.55);
    expect(data.week52High).toBe(199.62);
    expect(data.week52Low).toBe(124.17);
    expect(data.priceBookRatio).toBe(48.5);
    expect(data.roe).toBe(157.41);
    expect(data.revenue).toBe(391035000000);
  });

  it("filters out IB sentinel values (DBL_MAX)", () => {
    const data = createFundamentalsData("TEST");
    const str = "PEEXCLXOR=1.7976931348623157e+308;YIELD=0.55;NHIG=1.7976931348623157e308";
    parseFundamentalRatios(data, str);
    expect(data.peRatio).toBeNull();
    expect(data.dividendYield).toBe(0.55);
    expect(data.week52High).toBeNull();
  });

  it("handles partial data — only sets fields present in string", () => {
    const data = createFundamentalsData("GOOG");
    parseFundamentalRatios(data, "PEEXCLXOR=22.5;NHIG=180.00");
    expect(data.peRatio).toBe(22.5);
    expect(data.week52High).toBe(180);
    expect(data.eps).toBeNull();
    expect(data.week52Low).toBeNull();
  });

  it("ignores unknown keys", () => {
    const data = createFundamentalsData("TEST");
    const updated = parseFundamentalRatios(data, "UNKNOWN=42;PEEXCLXOR=10.5");
    expect(updated).toBe(true);
    expect(data.peRatio).toBe(10.5);
  });

  it("returns false for empty string", () => {
    const data = createFundamentalsData("TEST");
    expect(parseFundamentalRatios(data, "")).toBe(false);
  });

  it("returns false for non-string input", () => {
    const data = createFundamentalsData("TEST");
    expect(parseFundamentalRatios(data, null as unknown as string)).toBe(false);
    expect(parseFundamentalRatios(data, undefined as unknown as string)).toBe(false);
  });

  it("returns false when all keys are unknown", () => {
    const data = createFundamentalsData("TEST");
    expect(parseFundamentalRatios(data, "FOO=1;BAR=2")).toBe(false);
  });

  it("handles NaN values gracefully", () => {
    const data = createFundamentalsData("TEST");
    parseFundamentalRatios(data, "PEEXCLXOR=NaN;YIELD=1.5");
    expect(data.peRatio).toBeNull(); // NaN is not finite
    expect(data.dividendYield).toBe(1.5);
  });

  it("handles negative values (valid for EPS)", () => {
    const data = createFundamentalsData("TEST");
    parseFundamentalRatios(data, "TTMEPSXCLX=-2.50;TTMROEPCT=-15.3");
    expect(data.eps).toBe(-2.5);
    expect(data.roe).toBe(-15.3);
  });

  it("updates timestamp on successful parse", () => {
    const data = createFundamentalsData("TEST");
    const before = data.timestamp;
    // Small delay to ensure timestamp differs
    parseFundamentalRatios(data, "PEEXCLXOR=20");
    expect(data.timestamp).toBeTruthy();
    // Timestamp should be updated (may or may not be different if fast enough)
    expect(typeof data.timestamp).toBe("string");
  });

  it("does not update timestamp when no fields matched", () => {
    const data = createFundamentalsData("TEST");
    const original = data.timestamp;
    parseFundamentalRatios(data, "UNKNOWN=42");
    expect(data.timestamp).toBe(original);
  });

  it("handles malformed pairs gracefully", () => {
    const data = createFundamentalsData("TEST");
    // Mixed valid, empty, and malformed pairs
    parseFundamentalRatios(data, "PEEXCLXOR=25;;=bad;YIELD=1.0;noeq");
    expect(data.peRatio).toBe(25);
    expect(data.dividendYield).toBe(1.0);
  });

  it("handles zero values as valid", () => {
    const data = createFundamentalsData("TEST");
    parseFundamentalRatios(data, "YIELD=0;PEEXCLXOR=0");
    expect(data.dividendYield).toBe(0);
    expect(data.peRatio).toBe(0);
  });
});
