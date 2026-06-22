/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import MetricCards from "@/components/MetricCards";
import type { PortfolioData } from "@/lib/types";

afterEach(() => cleanup());

// Real IB snapshot 2026-06-22: 000660 SK Hynix, KRX/KRW, 1 sh @ ₩2,885,000.
function portfolio(unconverted: number): PortfolioData {
  return {
    source: "ib",
    bankroll: 100_000,
    peak_value: 100_000,
    last_sync: new Date().toISOString(),
    base_currency: "USD",
    fx_rates: { USD: 1 },
    fx_unconverted_count: unconverted,
    positions: [
      {
        id: 1,
        ticker: "000660",
        currency: "KRW",
        exchange: "KRX",
        structure: "Stock (1 share)",
        structure_type: "Stock",
        risk_profile: "equity",
        expiry: "N/A",
        contracts: 1,
        direction: "LONG",
        entry_cost: 2_764_000,
        max_risk: null,
        market_value: 2_885_000,
        legs: [
          {
            direction: "LONG",
            contracts: 1,
            type: "Stock",
            currency: "KRW",
            strike: null,
            entry_cost: 2_764_000,
            avg_cost: 2_764_000,
            market_price: 2_885_000,
            market_value: 2_885_000,
          },
        ],
        kelly_optimal: null,
        target: null,
        stop: null,
        entry_date: "unknown",
      },
    ],
    total_deployed_pct: 0,
    total_deployed_dollars: 0,
    remaining_capacity_pct: 100,
    position_count: 1,
    defined_risk_count: 0,
    undefined_risk_count: 1,
    avg_kelly_optimal: null,
  };
}

describe("MetricCards missing-FX warning", () => {
  it("shows a warning chip when fx_unconverted_count > 0", () => {
    render(
      <MetricCards
        portfolio={portfolio(1)}
        prices={{}}
        realizedPnl={0}
        section="portfolio"
      />,
    );
    expect(screen.getByText(/missing an FX rate/i)).toBeTruthy();
  });

  it("shows no warning when all positions converted", () => {
    render(
      <MetricCards
        portfolio={portfolio(0)}
        prices={{}}
        realizedPnl={0}
        section="portfolio"
      />,
    );
    expect(screen.queryByText(/missing an FX rate/i)).toBeNull();
  });
});
