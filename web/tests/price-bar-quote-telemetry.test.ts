import { createElement, type ComponentType } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { TickerQuoteTelemetry } from "../components/QuoteTelemetry";
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

function extractLabels(markup: string): string[] {
  return [...markup.matchAll(/<span class="price-bar-label">([^<]+)<\/span>/g)].map((match) => match[1]);
}

describe("Ticker quote telemetry", () => {
  it("renders BID, MID, ASK, then SPREAD and formats spread as raw dollars plus percent", () => {
    const priceData = makePriceData({
      symbol: "AAOI_20260320_105_C",
      bid: 13.8,
      ask: 16.2,
      last: 14.67,
      close: 25.16,
      volume: 232,
      high: 15.89,
      low: 14.8,
    });

    const html = renderToStaticMarkup(
      createElement(TickerQuoteTelemetry as unknown as ComponentType<{
        priceData: PriceData | null;
        label?: string;
      }>, {
        priceData,
        label: "AAOI 2026-03-20 $105 C",
      }),
    );

    expect(extractLabels(html).slice(1, 5)).toEqual(["BID", "MID", "ASK", "SPREAD"]);
    expect(html).toContain("$2.40 / 16.00%");
  });

  it("labels calculated quote values as MARK instead of LAST", () => {
    const priceData = makePriceData({
      symbol: "IWM",
      bid: 0.23,
      ask: 0.27,
      last: 0.25,
      lastIsCalculated: true,
    });

    const html = renderToStaticMarkup(
      createElement(TickerQuoteTelemetry as unknown as ComponentType<{
        priceData: PriceData | null;
        label?: string;
      }>, {
        priceData,
        label: "IWM Risk Reversal",
      }),
    );

    expect(extractLabels(html)).toContain("MARK");
    expect(html).not.toContain(">LAST<");
    expect(html).toContain("$0.25");
  });
});
