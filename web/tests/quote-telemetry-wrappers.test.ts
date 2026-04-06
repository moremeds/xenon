import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import {
  InstrumentOrderQuoteTelemetry,
  ModifyOrderQuoteTelemetry,
  TickerQuoteTelemetry,
} from "../components/QuoteTelemetry";
import type { PriceData } from "@/lib/pricesProtocol";

function makePriceData(overrides: Partial<PriceData> & { symbol: string }): PriceData {
  return {
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
    timestamp: new Date().toISOString(),
    ...overrides,
  };
}

function extractLabels(markup: string, className: string): string[] {
  const pattern = new RegExp(`<span class="${className}">([^<]+)</span>`, "g");
  return [...markup.matchAll(pattern)].map((match) => match[1]);
}

describe("Quote telemetry wrappers", () => {
  it("renders the ticker wrapper with raw spread dollars and percent", () => {
    const html = renderToStaticMarkup(
      createElement(TickerQuoteTelemetry, {
        priceData: makePriceData({
          symbol: "AMD_20270115_195_C",
          bid: 45.3,
          ask: 46.4,
          last: 45.75,
          close: 48.95,
          volume: 45,
          high: 46.1,
          low: 45,
        }),
        label: "AMD 2027-01-15 $195 C",
      }),
    );

    expect(extractLabels(html, "price-bar-label").slice(1, 5)).toEqual(["BID", "MID", "ASK", "SPREAD"]);
    expect(html).toContain("$1.10 / 2.40%");
  });

  it("renders the instrument-order wrapper with raw spread dollars and percent", () => {
    const html = renderToStaticMarkup(
      createElement(InstrumentOrderQuoteTelemetry, {
        priceData: makePriceData({
          symbol: "AAOI_20260320_105_C",
          bid: 13.8,
          ask: 16.2,
          last: 14.67,
          close: 25.16,
          volume: 232,
          high: 15.89,
          low: 14.8,
        }),
        label: "AAOI 2026-03-20 $105 C",
      }),
    );

    expect(extractLabels(html, "price-bar-label").slice(1, 5)).toEqual(["BID", "MID", "ASK", "SPREAD"]);
    expect(html).toContain("$2.40 / 16.00%");
  });

  it("renders the modify-order wrapper with raw spread dollars and percent", () => {
    const html = renderToStaticMarkup(
      createElement(ModifyOrderQuoteTelemetry, {
        priceData: makePriceData({
          symbol: "AAOI_20260320_105_C",
          bid: 12.8,
          ask: 14.4,
          last: 13.6,
          close: 15.16,
        }),
      }),
    );

    expect(extractLabels(html, "modify-market-label")).toEqual(["BID", "MID", "ASK", "SPREAD"]);
    expect(html).toContain("$1.60 / 11.76%");
  });
});
