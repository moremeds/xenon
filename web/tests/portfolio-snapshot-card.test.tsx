/**
 * @vitest-environment jsdom
 */

import React from "react";
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import {
  PortfolioSnapshotCard,
  type DashboardAccount,
} from "@/components/dashboard/PortfolioSnapshotCard";
import type { PortfolioData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";

afterEach(() => cleanup());

function ibPortfolio(): PortfolioData {
  return {
    source: "ib",
    bankroll: 1_000_000,
    peak_value: 1_000_000,
    last_sync: "2026-06-15T14:00:00Z",
    positions: [],
    total_deployed_pct: 1.3,
    total_deployed_dollars: 13267,
    remaining_capacity_pct: 98.7,
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
}

function futuPortfolio(): PortfolioData {
  return {
    source: "futu",
    bankroll: 233_563,
    peak_value: 233_563,
    last_sync: "2026-06-02T00:01:17Z",
    positions: [],
    total_deployed_pct: 71.2,
    total_deployed_dollars: 550_326,
    remaining_capacity_pct: 28.8,
    position_count: 39,
    defined_risk_count: 0,
    undefined_risk_count: 0,
    avg_kelly_optimal: null,
    account_summary: {
      net_liquidation: 233_563,
      daily_pnl: 9288,
      unrealized_pnl: 9288,
      realized_pnl: 0,
      settled_cash: -24_797,
      maintenance_margin: 0,
      excess_liquidity: 0,
      buying_power: 0,
      dividends: null,
    },
  };
}

function accounts(): DashboardAccount[] {
  return [
    {
      source: "ib",
      label: "IB",
      accountId: "IB Account",
      status: "live",
      portfolio: ibPortfolio(),
    },
    {
      source: "futu",
      label: "FUTU",
      accountId: "28175…3263",
      status: "stale",
      portfolio: futuPortfolio(),
    },
  ];
}

function mergedCell(
  container: HTMLElement,
  label: string,
): HTMLElement | undefined {
  return Array.from(container.querySelectorAll(".snapshot-cell")).find((el) =>
    el.textContent?.includes(label),
  ) as HTMLElement | undefined;
}

describe("PortfolioSnapshotCard (merged IB + FUTU)", () => {
  it("shows merged totals across both accounts by default", () => {
    const { container } = render(
      <PortfolioSnapshotCard accounts={accounts()} />,
    );
    // 1,003,726 + 233,563 = 1,237,289 -> $1.24M
    expect(mergedCell(container, "Net Liquidation")?.textContent).toContain(
      "$1.24M",
    );
    // -461 + 300(futu intraday w/o prices = 0 here) ... no prices passed -> futu today null
    // openRisk 13,267 + 550,326 = 563,593
    expect(mergedCell(container, "Open Risk")?.textContent).toContain(
      "$563,593",
    );
    // cash 990,229 + (-24,797) = 965,432
    expect(mergedCell(container, "Cash")?.textContent).toContain("$965,432");
  });

  it("merges Today P&L sign-correctly and tones it", () => {
    // No prices -> FUTU today is null (skipped); merged = IB -461 only
    const { container } = render(
      <PortfolioSnapshotCard accounts={accounts()} />,
    );
    const cell = mergedCell(container, "Today");
    expect(cell?.textContent).toContain("-$461");
    expect(cell?.querySelector(".snapshot-cell__value--fault")).toBeTruthy();
  });

  it("includes FUTU intraday P&L in the merge when prices are present", () => {
    const prices: Record<string, PriceData> = {};
    // give FUTU a position with live price so intraday computes
    const futu = futuPortfolio();
    futu.positions = [
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
    ];
    prices.TSLA = {
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
    };
    const accts: DashboardAccount[] = [
      {
        source: "ib",
        label: "IB",
        accountId: "IB",
        status: "live",
        portfolio: ibPortfolio(),
      },
      {
        source: "futu",
        label: "FUTU",
        accountId: "F",
        status: "stale",
        portfolio: futu,
      },
    ];
    const { container } = render(
      <PortfolioSnapshotCard accounts={accts} prices={prices} />,
    );
    // -461 (IB) + 300 (FUTU intraday) = -161
    expect(mergedCell(container, "Today")?.textContent).toContain("-$161");
  });

  it("hides the per-account breakdown until the toggle is clicked", () => {
    render(<PortfolioSnapshotCard accounts={accounts()} />);
    const toggle = screen.getByRole("button", { name: /breakdown/i });
    const body = document.getElementById("portfolio-breakdown")!;
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(body.hasAttribute("hidden")).toBe(true);

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(body.hasAttribute("hidden")).toBe(false);
    // per-account full net liq shown in the breakdown
    expect(body.textContent).toContain("IB");
    expect(body.textContent).toContain("$1,003,726");
    expect(body.textContent).toContain("FUTU");
    expect(body.textContent).toContain("$233,563");
  });

  it("renders --- for merged cells when all accounts are empty", () => {
    const empty: DashboardAccount[] = [
      {
        source: "ib",
        label: "IB",
        accountId: null,
        status: "down",
        portfolio: null,
      },
      {
        source: "futu",
        label: "FUTU",
        accountId: null,
        status: "down",
        portfolio: null,
      },
    ];
    const { container } = render(<PortfolioSnapshotCard accounts={empty} />);
    const text = container.textContent ?? "";
    expect(text).toContain("Net Liquidation");
    expect((text.match(/---/g) ?? []).length).toBeGreaterThanOrEqual(4);
  });
});
