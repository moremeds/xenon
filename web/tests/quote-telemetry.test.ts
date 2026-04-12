import { describe, it, expect } from "vitest";
import type { PriceData } from "../lib/pricesProtocol";
import {
  getQuoteMetrics,
  formatSpreadTelemetry,
  buildQuoteTelemetryModel,
} from "../lib/quoteTelemetry";

/** Factory for PriceData with sensible defaults — override only what matters. */
function makePriceData(overrides: Partial<PriceData> = {}): PriceData {
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
    timestamp: new Date().toISOString(),
    ...overrides,
  };
}

describe("getQuoteMetrics", () => {
  it("computes mid, spread, and spreadBps from bid/ask", () => {
    const result = getQuoteMetrics({ bid: 4.3, ask: 5.1 });
    expect(result.bid).toBe(4.3);
    expect(result.ask).toBe(5.1);
    expect(result.mid).toBe(4.7);
    expect(result.spread).toBe(0.8);
    // spreadBps = (0.80 / 4.70) * 10000 ≈ 1702
    expect(result.spreadBps).toBe(1702);
  });

  it("returns nulls when priceData is null", () => {
    const result = getQuoteMetrics(null);
    expect(result).toEqual({
      bid: null,
      mid: null,
      ask: null,
      spread: null,
      spreadBps: null,
    });
  });

  it("returns nulls when priceData is undefined", () => {
    const result = getQuoteMetrics(undefined);
    expect(result).toEqual({
      bid: null,
      mid: null,
      ask: null,
      spread: null,
      spreadBps: null,
    });
  });

  it("handles zero mid (spreadBps null)", () => {
    const result = getQuoteMetrics({ bid: 0, ask: 0 });
    expect(result.mid).toBe(0);
    expect(result.spread).toBe(0);
    expect(result.spreadBps).toBe(null); // mid <= 0
  });

  it("handles missing bid (partial data)", () => {
    const result = getQuoteMetrics({
      bid: undefined as unknown as number,
      ask: 5.1,
    });
    expect(result.bid).toBe(null);
    expect(result.mid).toBe(null);
    expect(result.spread).toBe(null);
  });
});

describe("formatSpreadTelemetry", () => {
  it("formats spread with percentage", () => {
    const result = formatSpreadTelemetry({ bid: 4.3, ask: 5.1 });
    // spread = 0.80, mid = 4.70, pct = (0.80/4.70)*100 = 17.02%
    expect(result).toContain("0.80");
    expect(result).toContain("17.02%");
  });

  it("returns --- for null input", () => {
    expect(formatSpreadTelemetry(null)).toBe("---");
  });
});

describe("buildQuoteTelemetryModel", () => {
  it("builds complete model from full price data", () => {
    const model = buildQuoteTelemetryModel(
      makePriceData({
        symbol: "AAPL",
        bid: 184.0,
        ask: 184.5,
        last: 184.22,
        close: 183.0,
        high: 185.0,
        low: 182.5,
        volume: 1234567,
      }),
    );
    expect(model).not.toBe(null);
    expect(model!.bid.value).toContain("184.00");
    expect(model!.ask.value).toContain("184.50");
    expect(model!.volume.value).toBe("1,234,567");
    expect(model!.day.tone).toBe("positive"); // 184.22 > 183.00
    expect(model!.day.trend).toBe("up");
  });

  it("returns null for null priceData", () => {
    expect(buildQuoteTelemetryModel(null)).toBe(null);
  });

  it("labels MARK when lastIsCalculated is true", () => {
    const model = buildQuoteTelemetryModel(
      makePriceData({
        last: 100,
        close: 100,
        lastIsCalculated: true,
      }),
    );
    expect(model!.last.label).toBe("MARK");
  });

  it("shows negative day change with down trend", () => {
    const model = buildQuoteTelemetryModel(
      makePriceData({
        last: 95,
        close: 100,
        volume: 0,
      }),
    );
    expect(model!.day.tone).toBe("negative");
    expect(model!.day.trend).toBe("down");
    expect(model!.day.value).toContain("-5.00%");
  });
});
