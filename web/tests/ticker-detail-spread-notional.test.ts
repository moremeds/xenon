import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import TickerDetailContent from "../components/TickerDetailContent";
import type { PortfolioData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";

vi.mock("../components/PriceChart", () => ({
  default: () => createElement("div", { "data-testid": "mock-price-chart" }),
}));

vi.mock("../components/ticker-detail/PositionTab", () => ({
  default: () => createElement("div", { "data-testid": "mock-position-tab" }),
}));

vi.mock("../components/ticker-detail/OrderTab", () => ({
  default: () => createElement("div", { "data-testid": "mock-order-tab" }),
}));

vi.mock("../components/ticker-detail/NewsTab", () => ({
  default: () => createElement("div", { "data-testid": "mock-news-tab" }),
}));

vi.mock("../components/ticker-detail/RatingsTab", () => ({
  default: () => createElement("div", { "data-testid": "mock-ratings-tab" }),
}));

vi.mock("../components/ticker-detail/SeasonalityTab", () => ({
  default: () => createElement("div", { "data-testid": "mock-seasonality-tab" }),
}));

vi.mock("../components/ticker-detail/CompanyTab", () => ({
  default: () => createElement("div", { "data-testid": "mock-company-tab" }),
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

const portfolio: PortfolioData = {
  bankroll: 100_000,
  peak_value: 100_000,
  last_sync: new Date().toISOString(),
  total_deployed_pct: 91.5,
  total_deployed_dollars: 91_500,
  remaining_capacity_pct: 8.5,
  position_count: 1,
  defined_risk_count: 1,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  exposure: {},
  violations: [],
  positions: [
    {
      id: 7,
      ticker: "AMD",
      structure: "Long Call",
      structure_type: "Long Call",
      direction: "LONG",
      contracts: 20,
      expiry: "2027-01-15",
      entry_date: "2026-03-01",
      entry_cost: 80_000,
      market_value: 91_500,
      market_price: 45.75,
      market_price_is_calculated: false,
      avg_cost: 40,
      risk_profile: "defined",
      target: null,
      stop: null,
      legs: [
        {
          direction: "LONG",
          contracts: 20,
          type: "Call",
          strike: 195,
          avg_cost: 4_000,
          entry_cost: 80_000,
          market_price: 45.75,
          market_price_is_calculated: false,
          market_value: 91_500,
        },
      ],
    },
  ],
};

const prices: Record<string, PriceData> = {
  AMD_20270115_195_C: makePriceData({
    symbol: "AMD_20270115_195_C",
    bid: 45.3,
    ask: 46.4,
    last: 45.75,
    close: 48.95,
    volume: 45,
    high: 46.1,
    low: 45,
  }),
};

const orders = {
  last_sync: new Date().toISOString(),
  open_orders: [],
  executed_orders: [],
  open_count: 0,
  executed_count: 0,
};

describe("TickerDetailContent spread telemetry", () => {
  it("shows raw spread dollars and percent on the shared price bar", () => {
    const html = renderToStaticMarkup(
      createElement(TickerDetailContent, {
        ticker: "AMD",
        positionId: 7,
        activeTab: "company",
        onTabChange: () => {},
        prices,
        fundamentals: {},
        portfolio,
        orders,
        theme: "dark",
      }),
    );

    expect(html).toContain("$1.10 / 2.40%");
    expect(html).not.toContain("$110.00 / 240 bps");
  });
});
