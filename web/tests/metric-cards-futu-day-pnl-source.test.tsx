/**
 * @vitest-environment jsdom
 */

import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import MetricCards from "@/components/MetricCards";
import type { PortfolioData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";

afterEach(() => cleanup());

const FUTU_PORTFOLIO: PortfolioData = {
  source: "futu",
  bankroll: 148000,
  peak_value: 148000,
  last_sync: new Date().toISOString(),
  positions: [
    {
      id: 1,
      ticker: "TSLA",
      structure: "Stock",
      structure_type: "Stock",
      risk_profile: "equity",
      expiry: "",
      contracts: 300,
      direction: "LONG",
      entry_cost: 96213,
      max_risk: null,
      market_value: 105501,
      legs: [
        {
          direction: "LONG",
          contracts: 300,
          type: "Stock",
          strike: null,
          entry_cost: 96213,
          avg_cost: 320.71,
          market_price: 351.67,
          market_value: 105501,
          market_price_is_calculated: false,
        },
      ],
      kelly_optimal: null,
      target: null,
      stop: null,
      entry_date: "2026-04-01",
    },
  ],
  total_deployed_pct: 71.2,
  total_deployed_dollars: 105501,
  remaining_capacity_pct: 28.8,
  position_count: 1,
  defined_risk_count: 0,
  undefined_risk_count: 1,
  avg_kelly_optimal: null,
  account_summary: {
    net_liquidation: 148000,
    daily_pnl: 9288,
    unrealized_pnl: 9288,
    realized_pnl: 0,
    settled_cash: -14585,
    maintenance_margin: 114285,
    excess_liquidity: 33715,
    buying_power: 29917,
    dividends: null,
  },
};

const PRICES: Record<string, PriceData> = {
  TSLA: {
    symbol: "TSLA",
    last: 351.67,
    lastIsCalculated: false,
    bid: 351.5,
    ask: 351.8,
    bidSize: 1,
    askSize: 1,
    volume: 100,
    high: null,
    low: null,
    open: null,
    close: 350.67,
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
  },
};

// Heavy render: MetricCards with a full Futu portfolio + prices map. Passes in
// isolation (~500ms) but saturates past the 5s default under full-suite
// parallelism. Bump the describe timeout rather than reshape the fixture.
describe("MetricCards Futu account Day P&L", { timeout: 15_000 }, () => {
  it("shows computed intraday day pnl instead of snapshot unrealized pnl and explains the live-price source", () => {
    const { container } = render(
      <MetricCards
        portfolio={FUTU_PORTFOLIO}
        prices={PRICES}
        realizedPnl={0}
        executedOrders={[]}
        section="portfolio"
      />,
    );

    const dayPnlCard = Array.from(
      container.querySelectorAll(".metric-card"),
    ).find((el) => el.textContent?.includes("Day P&L"));
    expect(dayPnlCard).toBeTruthy();
    expect(dayPnlCard?.textContent).toContain("+$300.00");
    expect(dayPnlCard?.textContent).not.toContain("+$9,288.00");

    fireEvent.click(dayPnlCard!);

    const modal = screen.getByRole("dialog");
    expect(modal.textContent).toContain("+$300.00");
    expect(modal.textContent).toContain("current_price");
    expect(modal.textContent).toContain(
      "Futu positions + live realtime prices",
    );

    const todayPnlSection = screen.getByText("TODAY'S P&L").closest("div");
    expect(todayPnlSection).toBeTruthy();

    const dayMoveCard = Array.from(
      container.querySelectorAll(".metric-card"),
    ).find((el) => el.textContent?.includes("Day Move"));
    expect(dayMoveCard?.textContent).toContain("+$300");
  });
});
