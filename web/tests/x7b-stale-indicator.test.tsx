/**
 * @vitest-environment jsdom
 */

import React from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import PositionTable from "../components/PositionTable";
import type { PortfolioPosition } from "../lib/types";
import type { PriceData } from "../lib/pricesProtocol";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("../components/InstrumentDetailModal", () => ({
  default: () => null,
}));

afterEach(cleanup);

// X7b stale indicator (G4): a row whose routed PriceData carries `stale:true`
// shows a minimal brand-token dot in the last-price cell. `---` rules are
// unchanged — a stale row with no price still renders "---".

const price = (over: Partial<PriceData>): PriceData => ({
  symbol: "X",
  last: 4.5,
  lastIsCalculated: false,
  bid: 4.4,
  ask: 4.6,
  bidSize: 1,
  askSize: 1,
  volume: 10,
  high: null,
  low: null,
  open: null,
  close: 4.25,
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
  ...over,
});

const STOCK_POSITION: PortfolioPosition = {
  id: 1,
  ticker: "AAPL",
  structure: "Stock",
  structure_type: "Stock",
  risk_profile: "equity",
  expiry: "N/A",
  contracts: 10,
  direction: "LONG",
  entry_cost: 1700,
  max_risk: 1700,
  market_value: 1800,
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "2026-03-01",
  legs: [
    {
      direction: "LONG",
      contracts: 10,
      type: "Stock",
      strike: null,
      entry_cost: 1700,
      avg_cost: 170,
      market_price: 180,
      market_value: 1800,
    },
  ],
};

const SPREAD_POSITION: PortfolioPosition = {
  id: 101,
  ticker: "CROX",
  structure: "Bull Call Spread",
  structure_type: "Bull Call Spread",
  risk_profile: "defined",
  expiry: "2026-04-17",
  contracts: 1,
  direction: "LONG",
  entry_cost: 325,
  max_risk: 325,
  market_value: 325,
  kelly_optimal: null,
  target: null,
  stop: null,
  entry_date: "2026-03-19",
  legs: [
    {
      direction: "LONG",
      contracts: 1,
      type: "Call",
      strike: 80,
      entry_cost: 300,
      avg_cost: 300,
      market_price: 4.5,
      market_value: 450,
    },
    {
      direction: "SHORT",
      contracts: 1,
      type: "Call",
      strike: 95,
      entry_cost: -25,
      avg_cost: -25,
      market_price: 1.25,
      market_value: -125,
    },
  ],
};

const SPREAD_PRICES: Record<string, PriceData> = {
  CROX_20260417_80_C: price({ symbol: "CROX_20260417_80_C" }),
  CROX_20260417_95_C: price({ symbol: "CROX_20260417_95_C", last: 1.25 }),
};

describe("PositionTable stale indicator", () => {
  it("marks the last-price cell of a stock row whose quote is stale", () => {
    render(
      <PositionTable
        positions={[STOCK_POSITION]}
        prices={{ AAPL: price({ symbol: "AAPL", last: 181, stale: true }) }}
      />,
    );
    const row = screen.getByText("AAPL").closest("tr");
    expect(row?.querySelector(".price-stale-dot")).not.toBeNull();
    expect(screen.getByLabelText("stale quote")).toBeTruthy();
    expect(row?.textContent).toContain("$181");
  });

  it("shows no indicator for a fresh quote", () => {
    render(
      <PositionTable
        positions={[STOCK_POSITION]}
        prices={{ AAPL: price({ symbol: "AAPL", last: 181 }) }}
      />,
    );
    expect(screen.queryByLabelText("stale quote")).toBeNull();
  });

  it("keeps '---' rendering and still flags stale when no price exists", () => {
    render(
      <PositionTable
        positions={[STOCK_POSITION]}
        prices={{
          AAPL: price({ symbol: "AAPL", last: null, stale: true }),
        }}
      />,
    );
    const row = screen.getByText("AAPL").closest("tr");
    expect(row?.textContent).toContain("—");
    expect(screen.getByLabelText("stale quote")).toBeTruthy();
  });

  it("flags an option row when any leg quote is stale and the expanded leg row", () => {
    render(
      <PositionTable
        positions={[SPREAD_POSITION]}
        prices={{
          ...SPREAD_PRICES,
          CROX_20260417_95_C: price({
            symbol: "CROX_20260417_95_C",
            last: 1.25,
            stale: true,
            source: "futu",
          }),
        }}
      />,
    );

    // Position row: one stale leg flags the merged last-price cell.
    expect(screen.getByLabelText("stale quote")).toBeTruthy();

    fireEvent.click(screen.getByLabelText("Expand legs for CROX"));
    const shortLeg = screen.getByText("SHORT 1x Call $95").closest("tr");
    const longLeg = screen.getByText("LONG 1x Call $80").closest("tr");
    expect(shortLeg?.querySelector(".price-stale-dot")).not.toBeNull();
    expect(longLeg?.querySelector(".price-stale-dot")).toBeNull();
  });
});
