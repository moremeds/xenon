/**
 * @vitest-environment jsdom
 */

import React from "react";
import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import type { PortfolioData, OrdersData } from "@/lib/types";

vi.mock("@/components/ChatPanel", () => ({
  default: () => <div data-testid="chat-panel-mock" />,
}));

import DashboardSurface from "@/components/dashboard/DashboardSurface";

afterEach(() => cleanup());

const PORTFOLIO: PortfolioData = {
  source: "ib",
  bankroll: 200000,
  peak_value: 200000,
  last_sync: "2026-06-15T14:00:00Z",
  positions: [],
  total_deployed_pct: 0,
  total_deployed_dollars: 0,
  remaining_capacity_pct: 100,
  position_count: 0,
  defined_risk_count: 0,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  account_summary: {
    net_liquidation: 148000,
    daily_pnl: 1234,
    unrealized_pnl: 0,
    realized_pnl: 0,
    settled_cash: 9000,
    maintenance_margin: 0,
    excess_liquidity: 0,
    buying_power: 0,
    dividends: null,
  },
};

const ORDERS: OrdersData = {
  last_sync: "2026-06-15T14:00:00Z",
  open_orders: [],
  executed_orders: [],
  open_count: 0,
  executed_count: 0,
};

describe("DashboardSurface", () => {
  it("renders portfolio card, orders card, and the chat rail", () => {
    const { container } = render(
      <DashboardSurface portfolio={PORTFOLIO} orders={ORDERS} />,
    );
    expect(container.querySelector(".dashboard-surface")).toBeTruthy();
    expect(container.querySelector(".dashboard-surface__rail")).toBeTruthy();
    expect(screen.getByText("Net Liquidation")).toBeTruthy();
    // "Working & Filled" appears twice — the collapsible section title AND the
    // card's panel-title — so getByText would throw on multiple matches.
    expect(screen.getAllByText("Working & Filled").length).toBeGreaterThan(0);
    expect(screen.getByTestId("chat-panel-mock")).toBeTruthy();
  });

  it("shows the orders empty state when orders is null (FUTU tab)", () => {
    render(<DashboardSurface portfolio={PORTFOLIO} orders={null} />);
    expect(screen.getByText("No open or filled orders today.")).toBeTruthy();
  });
});
