/**
 * @vitest-environment jsdom
 */

import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render } from "@testing-library/react";
import { PortfolioSnapshotCard } from "@/components/dashboard/PortfolioSnapshotCard";
import type { PortfolioData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";

afterEach(() => cleanup());

function ibPortfolio(dailyPnl: number | null): PortfolioData {
  return {
    source: "ib",
    bankroll: 200000,
    peak_value: 200000,
    last_sync: "2026-06-15T14:00:00Z",
    positions: [],
    total_deployed_pct: 52.7,
    total_deployed_dollars: 105501,
    remaining_capacity_pct: 47.3,
    position_count: 0,
    defined_risk_count: 0,
    undefined_risk_count: 0,
    avg_kelly_optimal: null,
    account_summary: {
      net_liquidation: 148000,
      daily_pnl: dailyPnl,
      unrealized_pnl: 0,
      realized_pnl: 0,
      settled_cash: 9000,
      maintenance_margin: 0,
      excess_liquidity: 0,
      buying_power: 0,
      dividends: null,
      cash: -14585,
    },
  };
}

const FUTU_PORTFOLIO: PortfolioData = {
  source: "futu",
  bankroll: 148000,
  peak_value: 148000,
  last_sync: "2026-06-15T14:00:00Z",
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
    timestamp: "2026-06-15T14:00:00Z",
  },
};

function todayCell(container: HTMLElement): HTMLElement | undefined {
  return Array.from(container.querySelectorAll(".snapshot-cell")).find((el) =>
    el.textContent?.includes("Today"),
  ) as HTMLElement | undefined;
}

describe("PortfolioSnapshotCard", () => {
  it("renders the four account cells from an IB portfolio", () => {
    const { container } = render(
      <PortfolioSnapshotCard portfolio={ibPortfolio(1234)} />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("Net Liquidation");
    expect(text).toContain("$148,000");
    expect(text).toContain("Open Risk");
    expect(text).toContain("$105,501");
    expect(text).toContain("Cash");
    expect(text).toContain("-$14,585");
    expect(text).toContain("Today");
    expect(text).toContain("+$1,234");
  });

  it("applies core tone to positive Today P&L", () => {
    const { container } = render(
      <PortfolioSnapshotCard portfolio={ibPortfolio(1234)} />,
    );
    const cell = todayCell(container);
    expect(cell?.querySelector(".snapshot-cell__value--core")).toBeTruthy();
  });

  it("applies fault tone to negative Today P&L", () => {
    const { container } = render(
      <PortfolioSnapshotCard portfolio={ibPortfolio(-890)} />,
    );
    const cell = todayCell(container);
    expect(cell?.querySelector(".snapshot-cell__value--fault")).toBeTruthy();
    expect(cell?.textContent).toContain("-$890");
  });

  it("applies neutral tone to null Today P&L", () => {
    const { container } = render(
      <PortfolioSnapshotCard portfolio={ibPortfolio(null)} />,
    );
    const cell = todayCell(container);
    expect(cell?.querySelector(".snapshot-cell__value--neutral")).toBeTruthy();
    expect(cell?.textContent).toContain("---");
  });

  it("uses FUTU intraday P&L from live prices, not snapshot daily_pnl", () => {
    const { container } = render(
      <PortfolioSnapshotCard portfolio={FUTU_PORTFOLIO} prices={PRICES} />,
    );
    const cell = todayCell(container);
    // (351.67 - 350.67) * 300 = +$300
    expect(cell?.textContent).toContain("+$300");
    expect(cell?.textContent).not.toContain("9,288");
  });

  it("renders --- for every cell when portfolio is null", () => {
    const { container } = render(<PortfolioSnapshotCard portfolio={null} />);
    const text = container.textContent ?? "";
    expect(text).toContain("Net Liquidation");
    expect((text.match(/---/g) ?? []).length).toBeGreaterThanOrEqual(4);
  });
});
