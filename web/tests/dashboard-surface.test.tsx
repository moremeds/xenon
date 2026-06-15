/**
 * @vitest-environment jsdom
 */

import React from "react";
import { describe, it, expect, afterEach, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import type { PortfolioData, OrdersData } from "@/lib/types";
import type { DashboardAccount } from "@/components/dashboard/PortfolioSnapshotCard";

vi.mock("@/components/ChatPanel", () => ({
  default: () => <div data-testid="chat-panel-mock" />,
}));

import DashboardSurface from "@/components/dashboard/DashboardSurface";

afterEach(() => cleanup());

const IB: PortfolioData = {
  source: "ib",
  bankroll: 1_000_000,
  peak_value: 1_000_000,
  last_sync: "2026-06-15T14:00:00Z",
  positions: [],
  total_deployed_pct: 0,
  total_deployed_dollars: 13267,
  remaining_capacity_pct: 100,
  position_count: 2,
  defined_risk_count: 0,
  undefined_risk_count: 0,
  avg_kelly_optimal: null,
  account_summary: {
    net_liquidation: 1_003_726,
    daily_pnl: -461,
    unrealized_pnl: 0,
    realized_pnl: 0,
    settled_cash: 990_229,
    maintenance_margin: 0,
    excess_liquidity: 0,
    buying_power: 0,
    dividends: null,
    cash: 990_229,
  },
};

const ACCOUNTS: DashboardAccount[] = [
  { source: "ib", label: "IB", accountId: "IB", status: "live", portfolio: IB },
  {
    source: "futu",
    label: "FUTU",
    accountId: "F",
    status: "stale",
    portfolio: null,
  },
];

const ORDERS: OrdersData = {
  last_sync: "2026-06-15T14:00:00Z",
  open_orders: [],
  executed_orders: [],
  open_count: 0,
  executed_count: 0,
};

describe("DashboardSurface", () => {
  it("renders the top strip (portfolio + orders) and a full-width chat", () => {
    const { container } = render(
      <DashboardSurface accounts={ACCOUNTS} orders={ORDERS} />,
    );
    expect(container.querySelector(".dashboard-surface")).toBeTruthy();
    expect(container.querySelector(".dashboard-surface__strip")).toBeTruthy();
    expect(container.querySelector(".dashboard-surface__chat")).toBeTruthy();
    expect(screen.getByText("Net Liquidation")).toBeTruthy();
    // "Working & Filled" appears twice (section title + card title)
    expect(screen.getAllByText("Working & Filled").length).toBeGreaterThan(0);
    expect(screen.getByTestId("chat-panel-mock")).toBeTruthy();
  });

  it("shows the orders empty state when orders is null", () => {
    render(<DashboardSurface accounts={ACCOUNTS} orders={null} />);
    expect(screen.getByText("No open or filled orders today.")).toBeTruthy();
  });
});
