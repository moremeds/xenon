/**
 * @vitest-environment jsdom
 *
 * MetricCards must convert each position's native day P&L to USD before the
 * TODAY'S P&L "Day Move" headline sums them. Regression for the observed
 * −$552,137 DAY MOVE, where JPY/KRW day P&L leaked into the USD total.
 *
 * Real 2026-06-22 IB snapshot: 000660 SK Hynix (KRX/KRW), 1 sh.
 *   ib_daily_pnl ₩121,000 ; USD.KRW 1537 → usd_per_unit[KRW] = 0.0006507
 *   → 121,000 × 0.0006507 = $78.73 (renders "+$79"), NOT "+$121,000".
 */
import { describe, it, expect, afterEach } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import MetricCards from "@/components/MetricCards";
import type { PortfolioData } from "@/lib/types";
import type { PriceData } from "@/lib/pricesProtocol";

afterEach(() => cleanup());

const makePrice = (overrides: Partial<PriceData>): PriceData =>
  ({
    symbol: "TEST",
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
    timestamp: "2026-06-22T06:00:00.000Z",
    ...overrides,
  }) as PriceData;

const portfolio: PortfolioData = {
  source: "ib",
  bankroll: 100_000,
  peak_value: 100_000,
  last_sync: "2026-06-22T06:00:00.000Z",
  base_currency: "USD",
  fx_rates: { USD: 1, KRW: 0.0006507 },
  fx_unconverted_count: 0,
  total_deployed_pct: 0,
  total_deployed_dollars: 1_877,
  remaining_capacity_pct: 100,
  position_count: 1,
  defined_risk_count: 0,
  undefined_risk_count: 1,
  avg_kelly_optimal: null,
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
      ib_daily_pnl: 121_000,
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
      entry_date: "2026-05-01",
    },
  ],
  account_summary: {
    net_liquidation: 100_000,
    daily_pnl: 78.73,
    unrealized_pnl: 0,
    realized_pnl: 0,
    settled_cash: 100_000,
    maintenance_margin: 0,
    excess_liquidity: 100_000,
    buying_power: 200_000,
    dividends: null,
  },
};

const prices: Record<string, PriceData> = {
  "000660": makePrice({ symbol: "000660", last: 2_885_000, close: 2_764_000 }),
};

describe("MetricCards — Day Move converts native day P&L to USD", () => {
  it("shows the KRW day P&L as ~$79 (USD), never the native ₩121,000", () => {
    render(
      <MetricCards
        portfolio={portfolio}
        prices={prices}
        realizedPnl={0}
        section="portfolio"
      />,
    );
    // USD-converted Day Move (and Total) render "+$79".
    expect(screen.getAllByText(/^\+\$79$/).length).toBeGreaterThan(0);
    // The native magnitude must NOT appear anywhere as a dollar figure.
    expect(screen.queryByText(/\$121,000/)).toBeNull();
  });
});
