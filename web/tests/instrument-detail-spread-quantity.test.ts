import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import InstrumentDetailModal from "../components/InstrumentDetailModal";
import type { PortfolioLeg } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";

vi.mock("../components/Modal", () => ({
  default: ({ children }: { children: React.ReactNode }) =>
    createElement("div", { "data-testid": "mock-modal" }, children),
}));

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

describe("InstrumentDetailModal spread telemetry", () => {
  it("shows raw spread dollars and percent", () => {
    const leg: PortfolioLeg = {
      direction: "LONG",
      contracts: 25,
      type: "Call",
      strike: 105,
      entry_cost: 22_775,
      avg_cost: 911,
      market_price: 14.67,
      market_price_is_calculated: false,
      market_value: 36_675,
    };

    const prices: Record<string, PriceData> = {
      AAOI_20260320_105_C: makePriceData({
        symbol: "AAOI_20260320_105_C",
        bid: 13.8,
        ask: 16.2,
        last: 14.67,
        close: 25.16,
      }),
    };

    const html = renderToStaticMarkup(
      createElement(InstrumentDetailModal, {
        leg,
        ticker: "AAOI",
        expiry: "2026-03-20",
        prices,
        onClose: () => {},
      }),
    );

    expect(html).toContain("$2.40 / 16.00%");
  });
});
